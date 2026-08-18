"""
Run the gate over stored leads and push the survivors to Google Sheets.

    python rungate.py --vertical "Emergency Plumbing"
    python rungate.py --vertical "Emergency Plumbing" --query "plumber in Manchester"
    python rungate.py --list

Reads the database and writes back to it; the only network calls are the
judge's (about $0.0002 a lead) and the Sheets write. Nothing here can send an
email — the gate decides who is worth contacting, never contacts anyone.

Safe to re-run. Gating is idempotent, and the sheet upserts on place_key, so a
second run updates rows rather than duplicating them.
"""

import argparse

from config import SHEETS, SHEETS_ENABLED
from core import llm, sheets
from core.logbook import get_logger, setup_logging
from qualifier import gate, profiles
from storage import gate_store, store

log = get_logger(__name__)


def _export(conn, vertical: str) -> None:
    """Push this vertical's leads to its tab, and all rejects to _Dropped.

    The tab is titled with the readable name, never the slug — passing
    'emergency-plumbing' through once created a second tab alongside
    'Emergency Plumbing' and quietly split the results in two."""
    if not SHEETS_ENABLED:
        log.info("sheets export off — GOOGLE_SHEETS_ID not set in .env")
        return
    profile = profiles.load(vertical)
    title = profiles.name_for(vertical)
    leads = gate_store.gated_leads(conn, profile["slug"], "lead")
    if leads:
        sheets.write_leads(title, gate_store.sheet_rows(leads))
    dropped = gate_store.all_dropped(conn)
    if dropped and SHEETS["write_dropped"]:
        sheets.write_leads(SHEETS["dropped_tab"], gate_store.sheet_rows(dropped))


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate stored leads.")
    parser.add_argument("--vertical", help="vertical name or slug")
    parser.add_argument("--query", help="gate one scrape query instead of a "
                                        "whole vertical")
    parser.add_argument("--list", action="store_true", help="list verticals")
    parser.add_argument("--no-sheets", action="store_true",
                        help="gate only, don't write to the spreadsheet")
    parser.add_argument("--rejudge", action="store_true",
                        help="ask the LLM again instead of reusing stored "
                             "verdicts — needed after editing a vertical's "
                             "profile, since the cached answer was formed "
                             "against the old prompt")
    args = parser.parse_args()

    setup_logging("gate")
    if args.list:
        for name in profiles.names():
            print(" ", name)
        return 0
    if not args.vertical:
        parser.error("--vertical is required (see --list)")

    conn = store.connect()
    leads = gate_store.leads_for_query(conn, args.query) if args.query else None
    before = llm.spent_usd()
    summary = gate.run(conn, args.vertical, leads, rejudge=args.rejudge)
    if not summary:
        conn.close()
        return 1

    if not args.no_sheets:
        _export(conn, args.vertical)
    conn.close()

    spent = llm.spent_usd() - before
    print(f"\n  {summary['in']} in -> {summary['leads']} leads "
          f"({summary['door1']} with evidence, {summary['door2']} structural), "
          f"{summary['dropped']} dropped")
    print(f"  judge cost this run: ${spent:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
