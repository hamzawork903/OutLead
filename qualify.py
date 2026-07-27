"""
Lead qualifier — Phase 2, stage 1. Run this after scraping (and ideally after
enrichment, though it works without it) to score every lead and decide which
service to pitch, BEFORE any outreach happens.

    python qualify.py                          # qualify every unscored lead
    python qualify.py "dentists in Austin, TX" # just this search's leads
    python qualify.py --force                  # re-score everything (after
                                                 # tuning QUALIFY in config.py)

Rule-based, no LLM, no network calls — instant. Safe to stop and re-run: only
unscored leads are processed by default. Results stream to the database AND a
live CSV as each lead is scored.
"""

import argparse
import sys
from datetime import datetime

from core import events
from core.logbook import get_logger, setup_logging
from qualifier.qualify import qualify
from storage import store

log = get_logger(__name__)

_CSV_COLUMNS = (["name", "quality_score", "service_fit", "fit_reason",
                 "qualify_status", "email", "email_status", "phone", "website"]
                + ["rating", "reviews", "open_state", "query", "maps_url"])


def _parse_args():
    p = argparse.ArgumentParser(
        description="Score and segment scraped leads before outreach.")
    p.add_argument("query", nargs="?", default=None,
                   help="only qualify leads from this search (default: all)")
    p.add_argument("--force", action="store_true",
                   help="re-score every lead, including already-qualified ones")
    p.add_argument("--limit", type=int, default=None, metavar="N")
    return p.parse_args()


def run_qualifier(conn, query=None, force=False, limit=None) -> dict:
    """Score every matching lead, streaming to DB + a live CSV. Returns a
    counts summary. Reusable by the CLI and (later) the web UI."""
    targets = store.leads_for_qualification(conn, query=query, force=force,
                                            limit=limit)
    total = len(targets)
    counts = {"total": total, "qualified": 0, "low_priority": 0, "skip": 0,
              "web_development": 0, "automation": 0, "ai": 0, "csv": None}
    if total == 0:
        log.info("  Nothing to qualify — every lead already has a score. "
                 "(Scrape more, or pass --force to re-score.)")
        return counts

    label = f'"{query}"' if query else "all leads"
    log.info("\n  Scoring %d leads (%s)...\n", total, label)

    writer = store.open_stream(query or "all-leads", _CSV_COLUMNS, kind="qualified")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for i, row in enumerate(targets, 1):
            result = qualify(row)
            store.save_qualification(conn, row["place_key"], result, now)
            counts[result.qualify_status] = counts.get(result.qualify_status, 0) + 1
            if result.service_fit:
                counts[result.service_fit] = counts.get(result.service_fit, 0) + 1

            out_row = dict(row)
            out_row.update(quality_score=result.quality_score,
                           service_fit=result.service_fit,
                           fit_reason=result.fit_reason,
                           qualify_status=result.qualify_status)
            writer.write(out_row)
            events.emit(events.LEAD_UPDATE, {
                "place_key": row["place_key"], "quality_score": result.quality_score,
                "service_fit": result.service_fit or "",
                "qualify_status": result.qualify_status,
            })

            tag = f"{result.qualify_status} · {result.service_fit or '-'} · score {result.quality_score}"
            if i % 10 == 0 or i == total or result.qualify_status == "qualified":
                log.info("  [%d/%d] %s  ->  %s", i, total, row["name"], tag)
    finally:
        writer.close()

    counts["csv"] = writer.path
    log.info("\n  Done: %d qualified, %d low priority, %d skipped.",
             counts["qualified"], counts["low_priority"], counts["skip"])
    log.info("  Service fit — web dev: %d | automation: %d | AI: %d",
             counts["web_development"], counts["automation"], counts["ai"])
    log.info("  Spreadsheet: %s", writer.path)
    return counts


def main() -> int:
    args = _parse_args()
    setup_logging("qualify" + (f"-{args.query}" if args.query else ""))
    conn = store.connect()
    try:
        run_qualifier(conn, query=args.query, force=args.force, limit=args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
