"""
Email enrichment — the second pass. Visits the website of every lead that has
one and hunts for a contact email, then records it.

Run it after scraping (any time — it reads from the database):

    python enrich.py                       # enrich every lead that needs it
    python enrich.py "dentists in Austin, TX"   # just this search's leads
    python enrich.py --limit 20            # quick sample

Safe to stop and re-run: only leads that haven't been enriched are processed,
so a re-run resumes where it left off. Sites are visited in parallel (they're
different servers, so this is both safe and fast). Results stream to the
database AND a live CSV as each site finishes — watch the spreadsheet fill up.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from config import ENRICH
from core import events
from core.logbook import get_logger, setup_logging
from enrichment import llm_writer, verify
from enrichment.harvester import EnrichResult, harvest_site
from storage import store

log = get_logger(__name__)

_SOCIAL_COLS = ["facebook", "instagram", "linkedin", "youtube", "tiktok", "twitter"]
_CSV_COLUMNS = (["name", "website_phones", "website", "email", "email_status",
                 "all_emails"]
                + _SOCIAL_COLS + ["email_source", "outcome"])


def _parse_args():
    p = argparse.ArgumentParser(description="Find contact emails for scraped leads.")
    p.add_argument("query", nargs="?", default=None,
                   help="only enrich leads from this search (default: all)")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="process at most N leads (quick sample)")
    p.add_argument("--workers", type=int, default=ENRICH["workers"], metavar="N",
                   help=f"parallel sites (default: {ENRICH['workers']})")
    return p.parse_args()


def run_enrichment(conn, query=None, limit=None, workers=None) -> dict:
    """Find emails for leads that need them, streaming results to the DB and a
    live CSV. Caller owns the connection. Returns a counts summary. Reusable by
    both the enrich CLI and batch mode.
    """
    workers = workers or ENRICH["workers"]
    targets = store.leads_needing_email(conn, query=query, limit=limit)
    total = len(targets)
    counts = {"enriched": 0, "no_email": 0, "site_dead": 0, "total": 0,
              "csv": None, "valid": 0, "risky": 0}
    if total == 0:
        log.info("  Nothing to enrich — every lead with a website has already "
                 "been checked.")
        return counts

    label = f'"{query}"' if query else "all leads"
    log.info("\n  Finding emails for %d leads (%s), %d sites at a time...\n",
             total, label, workers)

    writer = store.open_stream(query or "all-leads", _CSV_COLUMNS, kind="emails")
    now = datetime.now().isoformat(timespec="seconds")
    done = 0
    with_social = 0

    def _harvest_and_compose(lead_brief, website):
        """Runs in a worker thread: fetch the site, then (only if we found a
        deliverable email worth sending to) have the LLM write this lead's
        whole sequence — parallel with the other sites, so LLM latency never
        serializes the batch."""
        result = harvest_site(lead_brief["place_key"], website)
        email, email_status = verify.best_verified(result.all_emails)
        emails_json = None
        if email and email_status == "valid":
            emails_json = llm_writer.compose_emails(lead_brief, result.page_text)
        return result, email, email_status, emails_json

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for row in targets:
                place_key, name, website = row[0], row[1], row[2]
                brief = {"place_key": place_key, "name": name,
                         "category": row[3], "query": row[4],
                         "rating": row[5], "reviews": row[6],
                         "reviews_text": row[7] if len(row) > 7 else None}
                futures[pool.submit(_harvest_and_compose, brief, website)] = \
                    (place_key, name, website)
            for future in as_completed(futures):
                place_key, name, website = futures[future]
                try:
                    result, email, email_status, emails_json = future.result()
                except Exception:
                    log.exception("harvest crashed for %s — marking site_dead", website)
                    result = EnrichResult(place_key, None, status="site_dead")
                    email, email_status, emails_json = None, None, None

                socials = result.socials or {}
                phones = result.phones or []
                # Stream to DB + live CSV the instant this site is done.
                store.save_enrichment(conn, place_key, email, result.all_emails,
                                      result.source, result.status, socials,
                                      phones, email_status, now)
                if emails_json:
                    store.save_llm_emails(conn, place_key, emails_json, now)
                # Live update for the UI table (fills the email/social cells).
                events.emit(events.LEAD_UPDATE, {
                    "place_key": place_key, "email": email or "",
                    "email_status": email_status or "",
                    "all_emails": ", ".join(result.all_emails),
                    "website_phones": ", ".join(phones),
                    **{p: socials.get(p, "") for p in _SOCIAL_COLS},
                })
                row = {
                    "name": name, "website_phones": ", ".join(phones),
                    "website": website, "email": email or "",
                    "email_status": email_status or "",
                    "all_emails": ", ".join(result.all_emails),
                    "email_source": result.source or "",
                    "outcome": result.status,
                }
                row.update({p: socials.get(p, "") for p in _SOCIAL_COLS})
                writer.write(row)

                counts[result.status] = counts.get(result.status, 0) + 1
                if email_status == "valid":
                    counts["valid"] += 1
                elif email_status == "risky":
                    counts["risky"] += 1
                if socials:
                    with_social += 1
                done += 1
                if email or socials:
                    tag = f"{email} [{email_status}]" if email else "(no email)"
                    extra = f"  +{len(socials)} social" if socials else ""
                    log.info("  [%d/%d] %s  ->  %s%s", done, total, name, tag, extra)
                elif done % 10 == 0 or done == total:
                    log.info("  [%d/%d] processed...", done, total)
    finally:
        writer.close()

    counts["total"] = total
    counts["with_social"] = with_social
    counts["csv"] = writer.path
    found = counts["enriched"]
    rate = (found / total * 100) if total else 0
    log.info("\n  Emails: %d of %d sites (%.0f%% hit rate) — %d deliverable, "
             "%d risky. No email: %d | Unreachable: %d | With socials: %d",
             found, total, rate, counts["valid"], counts["risky"],
             counts["no_email"], counts["site_dead"], with_social)
    log.info("  Spreadsheet: %s", writer.path)
    return counts


def main() -> int:
    args = _parse_args()
    setup_logging("enrich" + (f"-{args.query}" if args.query else ""))
    conn = store.connect()
    try:
        counts = run_enrichment(conn, query=args.query, limit=args.limit,
                                workers=args.workers)
    finally:
        conn.close()
    if counts["total"] == 0:
        log.info("  (Scrape some leads first, or drop the query filter.)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped. Progress is saved — re-run to continue where you left off.")
        sys.exit(130)
