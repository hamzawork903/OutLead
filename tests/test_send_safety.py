"""The safety-critical tests. This software emails real people, so these
cover the guarantees that must never silently break:

  1. Nothing sends without BOTH keys of the gate.
  2. A dry run mutates nothing.
  3. Unwritten / unvalidated copy never reaches a recipient.
  4. Our own network failures don't burn a good lead.
  5. Suppressed and dead addresses are never contacted.

Offline: no network, no SMTP, temp DB only. Run: python tests/test_send_safety.py
"""
import json
import os
import smtplib
import socket
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logbook import setup_logging          # noqa: E402
setup_logging("test-send-safety")

passed = 0


def check(name, cond, detail=""):
    global passed
    assert cond, f"FAILED: {name} {detail}"
    passed += 1
    print(f"  ok: {name}")


def body(text="Their roofing work across Dallas is clearly busy this season. "):
    return text * 4 + "[BOOK_LINK]"


def emails_json(name="Baker Roofing"):
    return json.dumps({"greeting_name": name,
                       "steps": [{"subject": "missed calls", "body": body()}] * 3})


_db_counter = [0]


def fresh_db():
    """A throwaway DB per scenario. Unique filename each time — on Windows an
    open SQLite handle locks the file, so reusing one name fails."""
    from storage import store
    from core.models import Lead
    _db_counter[0] += 1
    path = os.path.join(tempfile.gettempdir(),
                        f"send_safety_test_{os.getpid()}_{_db_counter[0]}.db")
    if os.path.exists(path):
        os.remove(path)
    conn = store.connect(path)
    now = "2026-07-27T09:00:00"
    lead = Lead(place_key="k1", name="Baker Roofing", category="Roofer",
                address="a", phone="1", website="http://x.com", rating=4.8,
                reviews=62, maps_url="u", query="roofers in Dallas, TX")
    store.save_lead(conn, lead, now, vertical="commercial_contractor")
    store.save_enrichment(conn, "k1", "a@x.com", ["a@x.com"], "homepage",
                          "enriched", {}, [], "valid", now)
    store.enqueue_sequence(conn, "k1", "commercial_contractor", now)
    return conn, path, now


# ---------------------------------------------------- 1. the two-key gate ----
import config                                        # noqa: E402
from outreach import _resolve_dry_run, send_one_step  # noqa: E402
from storage import store                             # noqa: E402

with patch.dict(config.SEQUENCE, {"dry_run": True}):
    check("gate: dry_run=True refuses --live", _resolve_dry_run(True) == 2)
    check("gate: without --live it proceeds as a preview", _resolve_dry_run(False) == -1)

with patch.dict(config.SEQUENCE, {"dry_run": False}), \
     patch.object(config, "IDENTITY_IS_PLACEHOLDER", False), \
     patch("outreach.IDENTITY_IS_PLACEHOLDER", False):
    check("gate: both keys set -> allowed", _resolve_dry_run(True) == -1)

with patch.dict(config.SEQUENCE, {"dry_run": False}), \
     patch("outreach.IDENTITY_IS_PLACEHOLDER", True):
    check("gate: placeholder identity blocks live sending",
          _resolve_dry_run(True) == 2)

# ------------------------------------------- 2. dry run mutates nothing ----
conn, path, now = fresh_db()
store.save_llm_emails(conn, "k1", emails_json(), now)
before = conn.execute("SELECT current_step, status, next_send_at "
                      "FROM outreach_sequences WHERE place_key='k1'").fetchone()
row = store.due_sequences(conn, now)[0]
result = send_one_step(conn, row, dry_run=True, now=now)
after = conn.execute("SELECT current_step, status, next_send_at "
                     "FROM outreach_sequences WHERE place_key='k1'").fetchone()
check("dry run reports 'dry_run'", result == "dry_run", result)
check("dry run does NOT advance the step or change state",
      before == after, f"{before} -> {after}")
check("dry run leaves the lead still due", len(store.due_sequences(conn, now)) == 1)

# ------------------------------- 3. no generated copy -> skip, don't send ----
conn2, path2, now2 = fresh_db()          # no llm_emails saved
row2 = store.due_sequences(conn2, now2)[0]
with patch("sequencer.mailer.smtplib.SMTP") as smtp:
    r = send_one_step(conn2, row2, dry_run=False, now=now2)
check("lead with no written emails is skipped", r == "skipped", r)
check("...and SMTP was never even opened", not smtp.called)
check("...and the sequence stays active for a later backfill",
      conn2.execute("SELECT status FROM outreach_sequences WHERE place_key='k1'"
                    ).fetchone()[0] == "active")

# --------------------------- 4. our network failure must not burn a lead ----
store.save_llm_emails(conn2, "k1", emails_json(), now2)
row2 = store.due_sequences(conn2, now2)[0]
with patch("sequencer.mailer.smtplib.SMTP",
           side_effect=socket.gaierror(11001, "getaddrinfo failed")), \
     patch("sequencer.mailer._credentials", return_value=("me@x.com", "pw")), \
     patch("sequencer.mailer.time.sleep"):
    r = send_one_step(conn2, row2, dry_run=False, now=now2)
check("DNS/network failure returns 'retry_later'", r == "retry_later", r)
check("...and the lead stays active (not burned)",
      conn2.execute("SELECT status FROM outreach_sequences WHERE place_key='k1'"
                    ).fetchone()[0] == "active")
check("...and it's still in the due list", len(store.due_sequences(conn2, now2)) == 1)

# a genuinely refused address SHOULD stop the sequence
row2 = store.due_sequences(conn2, now2)[0]
with patch("sequencer.mailer.smtplib.SMTP",
           side_effect=smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no such user")})), \
     patch("sequencer.mailer._credentials", return_value=("me@x.com", "pw")):
    r = send_one_step(conn2, row2, dry_run=False, now=now2)
check("refused address returns 'failed'", r == "failed", r)
check("...and the sequence is stopped",
      conn2.execute("SELECT status FROM outreach_sequences WHERE place_key='k1'"
                    ).fetchone()[0] == "stopped")

# ------------------------------------- 5. suppression is always enforced ----
conn3, path3, now3 = fresh_db()
store.save_llm_emails(conn3, "k1", emails_json(), now3)
store.add_suppression(conn3, "a@x.com", "unsubscribe_reply", now3)
row3 = store.due_sequences(conn3, now3)[0]
with patch("sequencer.mailer.smtplib.SMTP") as smtp:
    r = send_one_step(conn3, row3, dry_run=False, now=now3)
check("suppressed address is skipped", r == "skipped", r)
check("...and SMTP was never opened for it", not smtp.called)

# --------------------------------------- 6. rendering never leaks/breaks ----
from sequencer import templates                       # noqa: E402
subj, text, html = templates.render_email("dental", 0, {"llm_emails": emails_json()})
check("render fills the booking link, no placeholder left",
      "[BOOK_LINK]" not in text and config.OUTREACH_IDENTITY["calendly_url"] in text)
check("footer carries an opt-out", "UNSUBSCRIBE" in text.upper())
check("no unfilled template braces", "{" not in html.split("<body")[1])
evil = json.dumps({"greeting_name": "Baker Roofing", "steps": [
    {"subject": "hi", "body": "Nice <script>alert(1)</script> work in Dallas. " * 4
                              + "[BOOK_LINK]"}] * 3})
_, _, ehtml = templates.render_email("dental", 0, {"llm_emails": evil})
check("injected markup is escaped in HTML",
      "<script>alert" not in ehtml and "&lt;script&gt;" in ehtml)
check("lead with no copy renders None (caller must skip)",
      templates.render_email("dental", 0, {}) is None)

for c, p in ((conn, path), (conn2, path2), (conn3, path3)):
    c.close()
    if os.path.exists(p):
        os.remove(p)

print(f"\nALL {passed} SEND-SAFETY CHECKS PASSED")
