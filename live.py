"""
Live pipeline — scrape, enrich, qualify, and (if enabled) send to each lead
the moment it's found, instead of running scraping/qualifying/outreach as
three separate batch passes on separate days. Built for the Terminal page
in the web UI, but works from the CLI too:

    python live.py "dentists in Austin, TX" --sync-qualify
    python live.py "dentists in Austin, TX" --sync-qualify --sync-send
    python live.py "dentists in Austin, TX" --limit 10 --sync-qualify --sync-send --live

Per lead, in order:
  1. Extract from Google Maps (name, category, phone, website, rating, ...).
  2. If a website is known and a sync stage below needs it: enrich it right
     then (find email, socials, phones) — the same harvester enrich.py uses,
     just called for one site instead of a batch.
  3. --sync-qualify: score the lead and pick a service fit immediately
     (qualifier.qualify(), same rules as qualify.py).
  4. --sync-send: if the lead just qualified, attempt to send step 0 of its
     sequence immediately (sequencer via outreach.send_one_step()).

SAFETY: --sync-send does NOT change whether a real email can go out — it
only changes WHEN a permitted send happens (right away instead of waiting
for a later `outreach.py` run). The exact same two-key gate applies: real
sending needs BOTH config.py's SEQUENCE["dry_run"] = False AND --live here.
Every stage narrates live via a SYNC event, so the terminal page can show
"found -> enriched -> qualified -> sent" per lead, as it happens.
"""

import argparse
import sys
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from core import events
from core.browser import maps_session
from core.logbook import get_logger, setup_logging
from core.reliability import BlockDetected
from enrichment import llm_writer, verify
from enrichment.harvester import harvest_site
from main import rate_gate, scrape_query
from outreach import _resolve_dry_run, send_one_step
from qualifier.qualify import qualify
from sequencer import sequence
from storage import campaigns, store

log = get_logger(__name__)


def _make_on_lead(sync_qualify: bool, sync_send: bool, dry_run: bool,
                  vertical=None):
    """Builds the per-lead callback wired into main.py's _extract_all().
    `vertical`, if set, is the email track every qualified lead this run gets."""

    def on_lead(conn, lead):
        events.emit(events.SYNC, {"stage": "found", "name": lead.name})

        # Only pay for enrichment if a later stage actually needs the result.
        if (sync_qualify or sync_send) and lead.website:
            result = harvest_site(lead.place_key, lead.website)
            email, email_status = verify.best_verified(result.all_emails)
            now = datetime.now().isoformat(timespec="seconds")
            store.save_enrichment(conn, lead.place_key, email, result.all_emails,
                                  result.source, result.status,
                                  result.socials or {}, result.phones or [],
                                  email_status, now)
            if email and email_status == "valid":
                emails_json = llm_writer.compose_emails(
                    store.lead_row(conn, lead.place_key), result.page_text)
                if emails_json:
                    store.save_llm_emails(conn, lead.place_key, emails_json, now)
            events.emit(events.SYNC, {"stage": "enriched", "name": lead.name,
                                      "email": email or ""})

        if not sync_qualify:
            return

        row = store.lead_row(conn, lead.place_key)
        verdict = qualify(row)
        now = datetime.now().isoformat(timespec="seconds")
        store.save_qualification(conn, lead.place_key, verdict, now)
        events.emit(events.SYNC, {
            "stage": "qualified", "name": lead.name,
            "score": verdict.quality_score, "fit": verdict.service_fit or "",
            "status": verdict.qualify_status,
        })

        if not sync_send or verdict.qualify_status != "qualified":
            return

        store.enqueue_sequence(conn, lead.place_key,
                               vertical or verdict.service_fit, now)
        seq_row = store.sequence_row(conn, lead.place_key)
        send_status = send_one_step(conn, seq_row, dry_run, now)
        events.emit(events.SYNC, {
            "stage": "sent", "name": lead.name,
            "email": seq_row.get("email") or "", "status": send_status,
        })

    return on_lead


def _parse_args():
    p = argparse.ArgumentParser(description="Live scrape -> enrich -> qualify -> send pipeline.")
    p.add_argument("query", help='e.g. "dentists in Austin, TX"')
    p.add_argument("--limit", type=int, default=None, metavar="N")
    p.add_argument("--fields", default=None, metavar="GROUPS")
    p.add_argument("--min-rating", type=float, default=None, metavar="R")
    p.add_argument("--with-website", action="store_true")
    p.add_argument("--skip-closed", action="store_true")
    p.add_argument("--lang", default=None, metavar="LOCALE")
    p.add_argument("--sync-qualify", action="store_true",
                   help="qualify each lead immediately as it's scraped")
    p.add_argument("--sync-send", action="store_true",
                   help="attempt to send each freshly-qualified lead immediately")
    p.add_argument("--live", action="store_true",
                   help="allow real sending (also requires SEQUENCE['dry_run']=False)")
    p.add_argument("--vertical", default=None, metavar="KEY",
                   help="tag every lead this run with a business vertical "
                        "(e.g. insurance, dental) — decides which email track")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    setup_logging(f"live-{args.query}")

    dry_run = True
    if args.sync_send:
        gate = _resolve_dry_run(args.live)
        if gate != -1:
            return gate
        dry_run = not args.live

    log.info('\nLive pipeline — "%s"\n', args.query)
    if args.sync_qualify:
        log.info("  sync: qualify each lead immediately")
    if args.sync_send:
        log.info("  sync: %s each qualified lead immediately",
                 "SEND (live)" if not dry_run else "dry-run send-preview for")

    conn = store.connect()
    # User-edited timing (Sequences page) wins over the config default.
    sequence.set_delays(campaigns.step_delays(conn))
    started = datetime.now().isoformat(timespec="seconds")
    run_leads, outcome = [], "error"
    try:
        stop = rate_gate(conn)
        if stop:
            log.warning(stop)
            outcome = "rate-capped"
            return 2

        groups = store.groups_from_arg(args.fields)
        columns = store.columns_for_groups(groups)
        filters = {"min_rating": args.min_rating,
                   "require_website": args.with_website,
                   "skip_closed": args.skip_closed}
        on_lead = _make_on_lead(args.sync_qualify, args.sync_send, dry_run,
                                args.vertical)

        with maps_session(locale=args.lang) as page:
            outcome = scrape_query(page, conn, args.query, run_leads,
                                   limit=args.limit, filters=filters,
                                   vertical=args.vertical,
                                   columns=columns, on_lead=on_lead)
        return 1 if outcome == "no-results" else 0

    except BlockDetected as block:
        log.info("\n  Stopped safely — Google challenged us (%s). Progress is "
                 "saved; re-run to resume.", block.kind)
        outcome = f"blocked:{block.kind}"
        return 3
    except PlaywrightTimeout:
        log.info("\n  The run timed out. Progress is saved; re-run to resume.")
        outcome = "timeout"
        return 1
    except KeyboardInterrupt:
        log.info("\n  Stopped by you. Progress is saved.")
        outcome = "interrupted"
        return 130
    except Exception:
        log.exception("unexpected error — full traceback in the log file")
        outcome = "error"
        return 1
    finally:
        ended = datetime.now().isoformat(timespec="seconds")
        try:
            store.record_run(conn, args.query, started, ended, len(run_leads), outcome)
        except Exception:
            log.debug("could not write run_log row", exc_info=True)
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
