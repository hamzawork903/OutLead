"""
Outreach engine — Phase 2, stage 2. Free email sequences with automatic
stop-on-reply. Run this anytime (or on a schedule) after qualify.py:

    python outreach.py                  # dry run: prints what it WOULD send
    python outreach.py --live           # sends for real (see safety gate below)
    python outreach.py "dentists in Austin, TX"   # only this search's leads
    python outreach.py --limit 10       # only enqueue 10 new leads this run
    python outreach.py --skip-replies   # don't check the inbox this pass

Each run does three things, in order:

  1. Enqueue newly-qualified leads (qualify_status='qualified', verified
     email, not already in a sequence) into a fresh 3-step sequence.
  2. Check the inbox for replies from anyone currently in an active sequence.
     A real human reply PAUSES that sequence for you to read — never
     auto-answered. "unsubscribe" permanently suppresses the address.
     Out-of-office auto-replies are ignored.
  3. Send whatever step is due for each remaining active sequence, up to the
     daily cap, then stop (rest stay due for the next run).

SAFETY: dry-run is a hard default. Real sending requires BOTH
config.SEQUENCE["dry_run"] = False in config.py AND --live on the command
line — a deliberate two-key gate so nothing sends by accident.
"""

import argparse
import os
import random
import sys
import time
from datetime import datetime

from config import IDENTITY_IS_PLACEHOLDER, SEQUENCE
from core.logbook import get_logger, setup_logging
from sequencer import inbox, mailer, sequence, templates
from storage import campaigns, store

log = get_logger(__name__)


def _parse_args():
    p = argparse.ArgumentParser(description="Run the free email outreach engine.")
    p.add_argument("query", nargs="?", default=None,
                   help="only enqueue leads from this search (default: all qualified)")
    p.add_argument("--live", action="store_true",
                   help="send for real (also requires SEQUENCE['dry_run']=False in config.py)")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="enqueue at most N new leads this run")
    p.add_argument("--skip-replies", action="store_true",
                   help="don't check the inbox this pass")
    p.add_argument("--vertical", default=None, metavar="KEY",
                   help="only touch this business type (e.g. dental, insurance) "
                        "-- both enqueueing and sending are restricted to it")
    p.add_argument("--count", type=int, default=None, metavar="N",
                   help="send to at most N leads this pass (with --vertical, "
                        "the Outreach page's send-by-type launcher)")
    p.add_argument("--latest", action="store_true",
                   help="with --count, take the most-recently-enqueued leads "
                        "first instead of the oldest-due-first default")
    p.add_argument("--campaign", default=None, metavar="QUERY",
                   help="only send to this campaign (the exact scrape query) "
                        "-- the Email Sender page's scope")
    p.add_argument("--order", default=None, choices=["latest", "oldest", "score"],
                   help="who goes first when --count caps the batch")
    return p.parse_args()


def _resolve_dry_run(live_flag: bool) -> int:
    """Two-key safety gate. Returns 0 to proceed, or an exit code to stop."""
    if not live_flag:
        return -1  # sentinel meaning "dry run, proceed"
    if SEQUENCE["dry_run"]:
        print("  Refusing to send for real: config.SEQUENCE['dry_run'] is still "
              "True.\n  Set it to False in config.py, then re-run with --live.")
        return 2
    if IDENTITY_IS_PLACEHOLDER:
        print("  Refusing to send for real: OUTREACH_IDENTITY is still the "
              "shipped placeholder.\n  Set OUTREACH_FROM_NAME, "
              "OUTREACH_BUSINESS_NAME and OUTREACH_BOOKING_URL in .env "
              "(see .env.example)\n  so your emails carry YOUR identity and "
              "YOUR booking link.")
        return 2
    return -1


def enqueue_new_leads(conn, query, limit, now, vertical=None) -> int:
    candidates = store.leads_ready_for_outreach(conn, query=query, vertical=vertical)
    if limit:
        candidates = candidates[:limit]
    for lead in candidates:
        # Route by the scrape-time vertical (the email track the operator chose);
        # fall back to the qualifier's service_fit for older, untagged leads.
        track = lead.get("vertical") or lead.get("service_fit")
        store.enqueue_sequence(conn, lead["place_key"], track, now)
    return len(candidates)


def check_replies(conn, now) -> dict:
    counts = {"replied": 0, "unsubscribed": 0, "ooo": 0}
    if not (os.environ.get("SMTP_EMAIL") and os.environ.get("SMTP_APP_PASSWORD")):
        log.info("  Skipping reply check — SMTP_EMAIL/SMTP_APP_PASSWORD not set.")
        return counts

    active = store.active_sequence_emails(conn)
    email_to_key = {email.lower(): key for key, email in active if email}
    if not email_to_key:
        return counts

    try:
        replies = inbox.fetch_replies(set(email_to_key), SEQUENCE["reply_check_window_days"])
    except Exception:
        log.exception("Reply check failed — continuing without it")
        return counts

    for r in replies:
        place_key = email_to_key.get(r["from_email"])
        if not place_key:
            continue
        kind = inbox.classify_reply(r["subject"], r["body"], r["headers"])
        if kind == "unsubscribe":
            store.add_suppression(conn, r["from_email"], "unsubscribe_reply", now)
            store.stop_sequence(conn, place_key, "unsubscribed", now)
            counts["unsubscribed"] += 1
            log.info("  %s unsubscribed — suppressed permanently.", r["from_email"])
        elif kind == "reply":
            store.stop_sequence(conn, place_key, "replied", now)
            counts["replied"] += 1
            snippet = " ".join(r["body"].split())[:200]
            log.info('  REAL REPLY from %s — sequence paused for you:\n'
                     '    "%s"', r["from_email"], snippet)
        else:
            counts["ooo"] += 1
            log.debug("  out-of-office from %s — ignoring", r["from_email"])
    return counts


def send_one_step(conn, row, dry_run: bool, now: str) -> str:
    """Send (or dry-run) whichever step is due for ONE sequence row. Handles
    every state transition (advance/complete/stop/log) itself, so callers —
    the batch loop below AND live.py's per-lead pipeline — never duplicate
    this logic. `row` needs: place_key, service_fit, current_step,
    first_sent_at, email, name, category. Returns one of: 'sent' | 'dry_run'
    | 'skipped' | 'failed' | 'completed'.

    A dry run is a pure PREVIEW: it logs what it would send but never
    advances/completes/stops the sequence. (It used to advance — which
    quietly consumed the whole campaign: after two preview passes every
    lead sat at step 2/3 with a future send date, and the first REAL email
    a lead would ever get was the 'just circling back' follow-up.)"""
    plan = sequence.plan_step(row["current_step"], row.get("first_sent_at"), now)
    if plan is None:
        if not dry_run:
            store.complete_sequence(conn, row["place_key"], now)
        return "completed"

    ok, reason = sequence.checklist(row["email"], store.is_suppressed(conn, row["email"]))
    if not ok:
        if not dry_run:
            store.stop_sequence(conn, row["place_key"], reason, now)
        store.log_send(conn, row["place_key"], plan.step, row["email"] or "",
                       "", "skipped", dry_run, now, error=reason)
        return "skipped"

    rendered = templates.render_email(row["service_fit"], plan.step, row)
    if rendered is None:
        # No LLM-written sequence for this lead yet (no API key when it was
        # enriched, or generation failed). Never substitute canned copy —
        # skip loudly and leave the sequence due for after a backfill.
        log.warning("  no generated emails for %r — skipped (run the LLM "
                    "backfill once OPENAI_API_KEY is set)", row.get("name"))
        store.log_send(conn, row["place_key"], plan.step, row["email"] or "",
                       "", "skipped", dry_run, now, error="no_generated_email")
        return "skipped"
    subject, body, html_body = rendered
    result = mailer.send_email(row["email"], subject, body, dry_run=dry_run,
                               html_body=html_body)
    store.log_send(conn, row["place_key"], plan.step, row["email"], subject,
                   result.status, dry_run, now, error=result.error)

    if result.status == "dry_run":
        return result.status                     # preview only — no state change
    if result.status == "retry_later":
        # OUR side broke (DNS/connection/timeout), not the address. Leave the
        # sequence active and due so the next pass retries it. (A 14-second
        # internet blip once killed 9 perfectly good leads permanently.)
        return "retry_later"
    if result.status == "sent":
        first_sent = now if row["current_step"] == 0 else row.get("first_sent_at")
        if plan.is_final:
            store.complete_sequence(conn, row["place_key"], now)
        else:
            store.advance_sequence(conn, row["place_key"], plan.step + 1,
                                   plan.next_send_at, now, first_sent_at=first_sent)
        return result.status
    else:
        store.stop_sequence(conn, row["place_key"], "send_failed", now)
        return "failed"


def send_due(conn, dry_run: bool, now: str, vertical=None, count=None,
            latest=False, campaign=None, order=None) -> dict:
    """Send (or preview) whatever's due. `vertical` restricts to one business
    type; `campaign` to one scrape query (the Email Sender page's scope);
    `count` caps how many get touched this pass; `order`/`latest` pick who
    goes first when capped (latest-enqueued / highest score / oldest-due)."""
    counts = {"sent": 0, "dry_run": 0, "skipped": 0, "failed": 0,
              "completed": 0, "retry_later": 0}
    today = now[:10]
    remaining = SEQUENCE["daily_send_cap"] - store.sent_today_count(conn, today)
    if not dry_run and remaining <= 0:
        log.info("  Daily send cap reached (%d/day) — nothing more sent this run.",
                 SEQUENCE["daily_send_cap"])
        return counts

    due = store.due_sequences(conn, now, vertical=vertical, latest=latest,
                              limit=count, query=campaign, order=order)
    for i, row in enumerate(due):
        if not dry_run and counts["sent"] >= remaining:
            log.info("  Daily send cap reached — %d lead(s) remain for next run.",
                     len(due) - i)
            break

        status = send_one_step(conn, row, dry_run, now)
        counts[status] += 1
        if status in ("sent", "dry_run") and not dry_run:
            lo, hi = SEQUENCE["between_sends_s"]
            time.sleep(random.uniform(lo, hi))

    return counts


def main() -> int:
    args = _parse_args()
    setup_logging("outreach" + (f"-{args.query}" if args.query else ""))

    gate = _resolve_dry_run(args.live)
    if gate != -1:
        return gate
    dry_run = not args.live

    now = datetime.now().isoformat(timespec="seconds")
    scope = f" — {args.vertical} only" if args.vertical else ""
    scope += f", up to {args.count} lead(s)" if args.count else ""
    scope += " (latest first)" if args.latest else ""
    log.info('\nOutreach run%s — %s\n', scope,
             "LIVE (real emails will send)" if not dry_run
             else "dry run (nothing will actually send)")

    conn = store.connect()
    try:
        # User-edited timing (Sequences page) wins over the config default.
        sequence.set_delays(campaigns.step_delays(conn))

        new_count = enqueue_new_leads(conn, args.query, args.limit, now,
                                      vertical=args.vertical)
        log.info("  Enqueued %d new lead(s) into sequences.", new_count)

        if not args.skip_replies:
            log.info("\n  Checking for replies...")
            reply_counts = check_replies(conn, now)
        else:
            reply_counts = {"replied": 0, "unsubscribed": 0, "ooo": 0}

        log.info("\n  Sending due steps...")
        send_counts = send_due(conn, dry_run, now, vertical=args.vertical,
                               count=args.count, latest=args.latest,
                               campaign=args.campaign, order=args.order)

        log.info("\n  ===== SUMMARY =====")
        log.info("  New sequences started : %d", new_count)
        log.info("  Replies received      : %d", reply_counts["replied"])
        log.info("  Unsubscribed          : %d", reply_counts["unsubscribed"])
        log.info("  Out-of-office ignored : %d", reply_counts["ooo"])
        label = "Would send (dry run)" if dry_run else "Sent"
        log.info("  %-22s : %d", label, send_counts["dry_run"] + send_counts["sent"])
        log.info("  Sequences completed   : %d", send_counts["completed"])
        log.info("  Skipped (no email/suppressed) : %d", send_counts["skipped"])
        log.info("  Failed                : %d", send_counts["failed"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
