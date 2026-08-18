"""
Batch mode — the "set it before bed, wake up to leads" runner. Scrapes many
niche x city searches in one go, then (optionally) finds emails for all of them.

Examples:
    python batch.py --niches "dentists,plumbers" --cities "Austin TX,Dallas TX"
    python batch.py --niches-file niches.txt --cities-file cities.txt --enrich
    python batch.py --niches "roofers" --cities "Houston TX" --limit 30 --enrich

How it behaves:
  - Builds one job per niche x city (e.g. 3 niches x 3 cities = 9 jobs).
  - Runs them through ONE browser session, pacing between jobs with the same
    rate discipline as single runs (cooldown + daily cap).
  - Each job is independent and resumable: a crash or a stop leaves finished
    jobs saved and the current job's queue 'pending', so re-running continues.
  - If Google challenges us, the whole batch stops cleanly (progress saved).
  - --enrich runs email enrichment across everything scraped, at the end.
"""

import argparse
import sys
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from core.browser import maps_session
from core.logbook import get_logger, setup_logging
from core.reliability import BlockDetected
from enrich import run_enrichment
from main import rate_gate, scrape_query
from storage import store

log = get_logger(__name__)


def build_queries(niches, cities, template="{niche} in {city}") -> list[str]:
    """Every niche x city combination as a search string (order: niche outer)."""
    queries = []
    for niche in niches:
        for city in cities:
            n, c = niche.strip(), city.strip()
            if n and c:
                queries.append(template.format(niche=n, city=c))
    return queries


def _read_terms(inline, path) -> list[str]:
    """Terms from a comma list (--x) or a file (--x-file, one per line,
    '#' comments and blanks ignored)."""
    if inline:
        return [t for t in inline.split(",") if t.strip()]
    if path:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
    return []


def _parse_args():
    p = argparse.ArgumentParser(description="Batch-scrape niche x city searches.")
    p.add_argument("--niches", help='comma list, e.g. "dentists,plumbers"')
    p.add_argument("--cities", help='comma list, e.g. "Austin TX,Dallas TX"')
    p.add_argument("--niches-file", help="file with one niche per line")
    p.add_argument("--cities-file", help="file with one city per line")
    p.add_argument("--terms", help='comma list of full search phrases used '
                   'as-is, e.g. "emergency plumbers open now,24h locksmith"')
    p.add_argument("--terms-file", help="file with one full search phrase per line")
    p.add_argument("--lang", default=None, metavar="LOCALE",
                   help='browser language/locale, e.g. "en-US", "es-ES"')
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="cap leads per job (quick/gentle runs)")
    p.add_argument("--min-rating", type=float, default=None, metavar="R",
                   help="keep only places rated at least R stars")
    p.add_argument("--with-website", action="store_true",
                   help="keep only places that have a website")
    p.add_argument("--skip-closed", action="store_true",
                   help="skip permanently/temporarily closed places")
    p.add_argument("--vertical", default=None, metavar="KEY",
                   help="tag all leads from this batch with a business vertical "
                        "(e.g. insurance, dental) — decides which email track "
                        "they get")
    p.add_argument("--enrich", action="store_true",
                   help="find emails for everything after scraping")
    p.add_argument("--fields", default=None, metavar="GROUPS",
                   help="comma list of field groups to collect (default: all)")
    p.add_argument("--workers", type=int, default=None, metavar="N",
                   help="parallel sites for the enrich step")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    niches = _read_terms(args.niches, args.niches_file)
    cities = _read_terms(args.cities, args.cities_file)
    terms = _read_terms(args.terms, args.terms_file)      # verbatim queries
    if (niches and not cities) or (cities and not niches):
        print("  --niches and --cities go together. Or use --terms for "
              "full search phrases.")
        return 2

    queries = build_queries(niches, cities) + terms
    # A vertical carries its own search terms and cities. Picking one should
    # mean "search all of them" — a single term against a single city returns
    # whatever Google has for that phrase (36 for private dentists in
    # Manchester), which is nowhere near the limit the operator asked for.
    if not queries and args.vertical:
        from qualifier import profiles
        queries = profiles.queries(args.vertical)
        if queries:
            log.info("  Vertical %r -> %d searches from its profile",
                     args.vertical, len(queries))
    if not queries:
        print("  Give me something to search. Examples:\n"
              '    python batch.py --niches "dentists,plumbers" --cities "Austin TX"\n'
              '    python batch.py --terms "emergency plumbers in Dallas TX"')
        return 2
    setup_logging(f"batch-{len(queries)}-jobs")
    log.info("\n  Batch: %d jobs (%d niches x %d cities)%s\n",
             len(queries), len(niches), len(cities),
             " + email enrichment" if args.enrich else "")
    for i, q in enumerate(queries, 1):
        log.info("    %2d. %s", i, q)

    groups = store.groups_from_arg(args.fields)
    columns = store.columns_for_groups(groups)
    enrich_after = args.enrich or (args.fields and store.wants_enrichment(groups))

    conn = store.connect()
    collected = 0          # running total, so --limit is a target not a per-job cap
    results = []
    stopped_early = False
    enrich_summary = None
    try:
        with maps_session(locale=args.lang) as page:
            for i, query in enumerate(queries, 1):
                stop = rate_gate(conn)          # cooldown between jobs + daily cap
                if stop:
                    log.warning(stop)
                    log.info("  Stopping the batch here — remaining %d jobs can "
                             "run later (they'll resume).", len(queries) - i + 1)
                    stopped_early = True
                    break

                # --limit is a target for the WHOLE batch, not per search. One
                # phrase in one city returns whatever Google has for it, so a
                # per-job cap of 500 would never be reached and the operator
                # would keep seeing 36.
                remaining = None
                if args.limit:
                    remaining = args.limit - collected
                    if remaining <= 0:
                        log.info("\n  Reached the %d-lead target — stopping with "
                                 "%d job(s) unrun (they resume next time).",
                                 args.limit, len(queries) - i + 1)
                        break
                log.info("\n  === Job %d/%d: %s ===  (%d/%s collected so far)",
                         i, len(queries), query, collected,
                         args.limit or "no target")
                started = datetime.now().isoformat(timespec="seconds")
                run_leads, outcome = [], "error"
                filters = {"min_rating": args.min_rating,
                           "require_website": args.with_website,
                           "skip_closed": args.skip_closed}
                try:
                    outcome = scrape_query(page, conn, query, run_leads,
                                           limit=remaining, filters=filters,
                                           columns=columns,
                                           vertical=args.vertical,
                                           want_reviews="reviews_text" in groups)
                except BlockDetected as block:
                    outcome = f"blocked:{block.kind}"
                    log.warning("  Google challenged us (%s) — stopping the whole "
                                "batch. Progress saved; resume later.", block.kind)
                    store.record_run(conn, query, started,
                                     datetime.now().isoformat(timespec="seconds"),
                                     len(run_leads), outcome)
                    results.append((query, len(run_leads), outcome))
                    stopped_early = True
                    break
                except (PlaywrightTimeout, Exception) as err:
                    # One bad job must not kill the batch — log and move on.
                    outcome = "error"
                    log.warning("  Job failed (%s: %s) — skipping to the next.",
                                type(err).__name__, err)
                finally:
                    collected += len(run_leads)
                    if not outcome.startswith("blocked"):
                        store.record_run(conn, query, started,
                                         datetime.now().isoformat(timespec="seconds"),
                                         len(run_leads), outcome)
                        results.append((query, len(run_leads), outcome))

        # Email enrichment across everything scraped (safe even after a block —
        # it hits business sites, not Google).
        if enrich_after:
            log.info("\n  === Email enrichment ===")
            enrich_summary = run_enrichment(conn, workers=args.workers)
    finally:
        conn.close()

    _print_summary(results, stopped_early, enrich_summary)
    return 0


def _print_summary(results, stopped_early, enrich_summary) -> None:
    total_leads = sum(n for _, n, _ in results)
    log.info("\n  ===== BATCH SUMMARY =====")
    for query, n, outcome in results:
        tag = "" if outcome == "ok" else f"  ({outcome})"
        log.info("    %-40s %4d leads%s", query[:40], n, tag)
    log.info("    %-40s %4d leads", "TOTAL", total_leads)
    if stopped_early:
        log.info("  (Batch stopped early — re-run the same command to finish "
                 "the rest; done jobs are skipped, partial ones resume.)")
    if enrich_summary and enrich_summary.get("total"):
        log.info("  Emails: %d found of %d sites checked.",
                 enrich_summary["enriched"], enrich_summary["total"])
    log.info("  Spreadsheets are in exports/ (one per job, written live).")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Batch stopped. Done jobs are saved; re-run to resume the rest.")
        sys.exit(130)
