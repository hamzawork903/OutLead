"""
Google Maps lead scraper — entry point.

Usage:
    python main.py "dentists in Austin, TX"
    python main.py "dentists in Austin, TX" --limit 10   # quick, gentle run
    python main.py "dentists in Austin, TX" --fresh       # ignore a saved resume
    python main.py "dentists in Austin, TX" --keep-open

Pipeline: rate check -> search -> collect listing URLs -> open each and extract
lead fields -> dedupe into SQLite -> CSV. Hardened for long unattended runs:
retry-with-backoff, block detection (stops cleanly), crash-resume via a work
queue, and rate discipline (cooldown + daily cap).
"""

import argparse
import re
import sys
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

import re as _re

from config import LOGS_DIR, RATE
from core import events, humanizer
from core.browser import maps_session
from core.ids import place_key_from_url
from core.logbook import get_logger, setup_logging
from core.models import Listing
from core.reliability import BlockDetected
from scraper.collector import collect_listings
from scraper.extractor import extract_lead, lead_passes
from scraper.search import run_search
from storage import store

_PLACE_ID_RE = _re.compile(r"^ChIJ[\w\-]{10,}$")

log = get_logger(__name__)


def save_listing_file(query: str, listings) -> str:
    """Write name + URL per line so the operator can eyeball the haul."""
    LOGS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:50]
    path = LOGS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for listing in listings:
            f.write(f"{listing.name}\n{listing.url}\n\n")
    return str(path)


def rate_gate(conn) -> str | None:
    """Enforce rate discipline. Returns a stop-reason, or None to proceed
    (waiting out any remaining between-run cooldown first)."""
    today = datetime.now().strftime("%Y-%m-%d")
    done = store.extracted_today(conn, today)
    if done >= RATE["daily_listing_cap"]:
        return (f"Daily safety cap reached ({done} leads today, cap is "
                f"{RATE['daily_listing_cap']}). Stopping to keep the IP/account "
                f"safe. Try tomorrow, or raise RATE['daily_listing_cap'] in "
                f"config.py.")
    last = store.last_run_ended(conn)
    if last:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            gap = RATE["between_runs_s"]
        remaining = RATE["between_runs_s"] - gap
        if remaining > 0:
            log.info("  Cooling down %.0fs between runs (rate safety)...",
                     remaining)
            humanizer.pause_range(max(0.1, remaining * 0.98), remaining)
    return None


def _extract_all(page, conn, listings, query, run_leads, writer,
                 crash_after=None, filters=None, on_lead=None, vertical=None,
                 want_reviews=False):
    """Open each listing, extract, apply filters, store with dedupe, mark the
    queue, and STREAM each kept lead to the CSV as it's found.

    Appends each kept Lead to `run_leads` (a caller-owned list) as it goes, so
    even if this raises mid-way the caller's `finally` sees accurate partial
    progress for rate accounting. Writes to DB + CSV and marks the queue
    per-lead, so a crash leaves done work safe and the rest 'pending' for a
    resume. Returns new_count. May raise BlockDetected (caller stops cleanly).

    on_lead(conn, lead), if given, runs synchronously right after each lead
    is saved — this is live.py's hook for inline enrich -> qualify -> send.
    Every normal caller (main.py's CLI, batch.py) leaves this None, so nothing
    about their tested behavior changes.
    """
    from dataclasses import asdict

    filters = filters or {}
    now = datetime.now().isoformat(timespec="seconds")
    new_count, filtered = 0, 0
    total = len(listings)
    for i, listing in enumerate(listings, 1):
        if crash_after is not None and i > crash_after:
            raise RuntimeError(
                f"simulated crash after {crash_after} listings (--crash-after)")
        events.emit(events.CURSOR, {"name": listing.name, "i": i, "total": total})
        lead = extract_lead(page, listing, query, want_reviews=want_reviews)
        if lead is None:
            store.mark_queue(conn, query, listing.place_key, "failed")
            continue
        keep, reason = lead_passes(lead, **filters)
        if not keep:
            filtered += 1
            store.mark_queue(conn, query, listing.place_key, "filtered")
            log.debug("filtered out %r (%s)", lead.name, reason)
            if i < total:
                humanizer.pause("between_listings")
            continue
        if store.save_lead(conn, lead, now, vertical=vertical):
            new_count += 1
        store.mark_queue(conn, query, listing.place_key, "done")
        row = asdict(lead)
        # CSV/UI want a flat string, not a list of dicts.
        row["reviews_text"] = " | ".join(
            f'{r.get("date") or r.get("when") or "?"} · '
            f'{r.get("stars") or "?"}★ {r.get("text", "")}'
            for r in (lead.reviews_text or []))
        writer.write(row)            # live: this row hits the spreadsheet now
        events.emit(events.LEAD, row)  # live: this row hits the UI table now
        run_leads.append(lead)
        if on_lead is not None:
            on_lead(conn, lead)
        if i % 5 == 0 or i == total:
            log.info("  ...%d of %d listings done", i, total)
        if i < total:
            humanizer.pause("between_listings")
    if filtered:
        log.info("  Skipped %d place(s) that didn't match your filters.", filtered)
    return new_count


def scrape_query(page, conn, query, run_leads, *, limit=None, fresh=False,
                 crash_after=None, filters=None, columns=None, on_lead=None,
                 vertical=None, want_reviews=False) -> str:
    """Scrape one query on an already-open Maps page. Appends Leads to the
    caller-owned `run_leads` and streams them to a live CSV. Returns an outcome
    string ('ok' | 'single-place' | 'no-results'). May raise BlockDetected /
    PlaywrightTimeout for the caller to handle. Reused by both main() and batch.
    """
    pending = [] if fresh else store.pending_items(conn, query)
    if pending:
        if limit:
            pending = pending[:limit]
        log.info("  Resuming an earlier run — %d listings still to do "
                 "(skipping search + scroll).", len(pending))
        listings = pending
    else:
        result = run_search(page, query)
        if result.kind == "single_place":
            log.info("\n  Your search matched exactly one business, so Maps "
                     "opened its page directly. Try a broader query like "
                     '"dentists in Austin" to get a full list.')
            return "single-place"
        if result.kind == "no_results":
            log.info("\n  Google Maps found nothing for this search. Check the "
                     "spelling, or try a nearby bigger city.")
            return "no-results"

        log.info("  Results are in — collecting the list (scrolls slowly on "
                 "purpose, to look human)...")
        listings, complete = collect_listings(page, target=limit)
        save_listing_file(query, listings)
        if limit:
            listings = listings[:limit]
        now = datetime.now().isoformat(timespec="seconds")
        store.clear_queue(conn, query)
        store.enqueue(conn, query, listings, now)
        note = ("complete list" if (complete or limit)
                else "feed stalled early — re-run to fill gaps")
        log.info("  Collected %d (%s). Opening each to pull contact details...",
                 len(listings), note)

    writer = store.open_stream(query, columns or store._CSV_COLUMNS)
    try:
        new_count = _extract_all(page, conn, listings, query, run_leads, writer,
                                 crash_after, filters, on_lead=on_lead,
                                 vertical=vertical, want_reviews=want_reviews)
    finally:
        writer.close()   # always leave a valid CSV, even on a crash
    already = len(run_leads) - new_count
    log.info("\n  Done: %d leads (%d new, %d already in the database).",
             len(run_leads), new_count, already)
    log.info("  Spreadsheet (written live): %s", writer.path)
    return "ok"


def scrape_url(page, conn, url_or_id, run_leads, *, filters=None,
               columns=None, vertical=None) -> str:
    """Scrape starting from a pasted Google Maps URL or a place ID, instead of
    a text search. Handles a single place (place URL or ChIJ id) or a Maps
    search URL (collect the whole feed)."""
    query = url_or_id                       # grouping key for storage/CSV
    target = url_or_id.strip()

    if _PLACE_ID_RE.match(target):
        place_url = f"https://www.google.com/maps/place/?q=place_id:{target}"
        listings = [Listing(name="(place id)", url=place_url, place_key=target)]
        log.info("  Looking up place ID %s...", target)
    elif "/maps/place/" in target:
        listings = [Listing(name="(place)", url=target,
                            place_key=place_key_from_url(target))]
        log.info("  Opening the pasted place URL...")
    else:
        log.info("  Opening the Maps URL and collecting results...")
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        listings, _ = collect_listings(page)

    if not listings:
        log.info("\n  Nothing found at that URL.")
        return "no-results"

    now = datetime.now().isoformat(timespec="seconds")
    store.clear_queue(conn, query)
    store.enqueue(conn, query, listings, now)
    log.info("  Extracting %d place(s)...", len(listings))
    writer = store.open_stream(query, columns or store._CSV_COLUMNS)
    try:
        _extract_all(page, conn, listings, query, run_leads, writer, None, filters,
                     vertical=vertical)
    finally:
        writer.close()
    log.info("\n  Done: %d lead(s). Spreadsheet (written live): %s",
             len(run_leads), writer.path)
    return "ok"


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape business leads from Google Maps.")
    parser.add_argument(
        "query", nargs="?", default=None,
        help='what to search, e.g. "dentists in Austin, TX" (niche + city)')
    parser.add_argument(
        "--url", default=None,
        help="start from a Google Maps URL or place ID instead of a text search")
    parser.add_argument(
        "--lang", default=None, metavar="LOCALE",
        help='browser language/locale, e.g. "en-US", "es-ES"')
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="stop after N leads — a quick, gentle run (default: full list)")
    parser.add_argument(
        "--fresh", action="store_true",
        help="ignore any saved resume queue and scrape this query from scratch")
    parser.add_argument(
        "--min-rating", type=float, default=None, metavar="R",
        help="keep only places rated at least R stars (e.g. 4.5)")
    parser.add_argument(
        "--with-website", action="store_true",
        help="keep only places that have a website")
    parser.add_argument(
        "--skip-closed", action="store_true",
        help="skip permanently/temporarily closed places")
    parser.add_argument(
        "--enrich", action="store_true",
        help="find emails/socials for the scraped leads afterwards")
    parser.add_argument(
        "--fields", default=None, metavar="GROUPS",
        help="comma list of field groups to collect (default: all). Groups: "
             "basics,phone_website,ratings,hours_price,emails,socials,"
             "website_phones. Enrichment runs only if emails/socials/"
             "website_phones is included.")
    parser.add_argument(
        "--vertical", default=None, metavar="KEY",
        help="tag every lead from this run with a business vertical "
             "(e.g. insurance, dental) — decides which email track they get")
    parser.add_argument(
        "--keep-open", action="store_true",
        help="leave the browser open after the run so you can inspect the page")
    # Hidden: simulate a crash after N listings, to test crash-resume.
    parser.add_argument("--crash-after", type=int, default=None,
                        help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    target = args.query or args.url
    if not target:
        print('  Give me something to scrape:\n'
              '    python main.py "dentists in Austin, TX"\n'
              '    python main.py --url "https://www.google.com/maps/place/..."')
        return 2
    log_path = setup_logging(target)
    log.info('\nGoogle Maps lead scraper — "%s"\n', target)
    log.debug("full log for this run: %s", log_path)

    conn = store.connect()
    started = datetime.now().isoformat(timespec="seconds")
    run_leads, outcome, verdict = [], "error", 1
    try:
        stop = rate_gate(conn)
        if stop:
            log.warning(stop)
            outcome, verdict = "rate-capped", 2
            return verdict

        filters = {"min_rating": args.min_rating,
                   "require_website": args.with_website,
                   "skip_closed": args.skip_closed}
        groups = store.groups_from_arg(args.fields)
        columns = store.columns_for_groups(groups)
        enrich_after = args.enrich or (args.fields and store.wants_enrichment(groups))

        with maps_session(locale=args.lang) as page:
            if args.url:
                outcome = scrape_url(page, conn, args.url, run_leads,
                                     filters=filters, columns=columns,
                                     vertical=args.vertical)
            else:
                outcome = scrape_query(page, conn, args.query, run_leads,
                                       limit=args.limit, fresh=args.fresh,
                                       crash_after=args.crash_after,
                                       filters=filters, columns=columns,
                                       vertical=args.vertical,
                                       want_reviews="reviews_text" in groups)
            verdict = 1 if outcome == "no-results" else 0

            if enrich_after and outcome == "ok":
                from enrich import run_enrichment
                log.info("\n  === Email enrichment ===")
                run_enrichment(conn, query=target)

            if args.keep_open:
                log.info("\n  Browser stays open (--keep-open). Press Enter "
                         "here to finish.")
                input()
            return verdict

    except BlockDetected as block:
        log.info("\n  Stopped safely — Google challenged us (%s). Your progress "
                 "is saved. Wait a while, then re-run the SAME command to resume "
                 "where it left off.", block.kind)
        outcome, verdict = f"blocked:{block.kind}", 3
        return verdict
    except PlaywrightTimeout:
        log.info("\n  The run timed out — details and a screenshot are in the "
                 "log: %s. Progress is saved; re-run to resume.", log_path)
        outcome, verdict = "timeout", 1
        return verdict
    except KeyboardInterrupt:
        log.info("\n  Stopped by you. Progress is saved; re-run to resume.")
        outcome, verdict = "interrupted", 130
        return verdict
    except Exception:
        log.exception("unexpected error — full traceback in the log file")
        log.info("\n  Something unexpected went wrong. The exact error is in the "
                 "log: %s. Progress is saved; re-run to resume.", log_path)
        outcome, verdict = "error", 1
        return verdict
    finally:
        ended = datetime.now().isoformat(timespec="seconds")
        try:
            store.record_run(conn, target, started, ended,
                             len(run_leads), outcome)
        except Exception:
            log.debug("could not write run_log row", exc_info=True)
        conn.close()
        log.info("\n  Full log for this run: %s", log_path)


if __name__ == "__main__":
    sys.exit(main())
