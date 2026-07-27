"""
Reply detection. Two separate concerns:

  classify_reply()  — pure text/header classification, no network, testable
                      with plain fixtures.
  fetch_replies()   — the IMAP mechanics: connect, search since a date, pull
                      out messages from addresses we're actively sequencing.

No AI, no paid classification API. The categories are deliberately simple and
conservative:

  "unsubscribe" — contains an opt-out phrase -> permanently suppress
  "ooo"         — an out-of-office auto-reply -> ignore, sequence continues
  "reply"       — anything else -> a real human replied -> STOP for review

A genuine reply is never auto-answered or auto-classified by sentiment — it's
handed to you. That's both the cheapest and the safest default.
"""

import email
import imaplib
import re
import socket
import threading
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr

from config import SMTP
from core.logbook import get_logger
from sequencer.mailer import _credentials

log = get_logger(__name__)

_UNSUB_RE = re.compile(
    r"\b(unsubscribe|remove me|stop emailing|do not (contact|email) me|"
    r"take me off|opt[- ]?out)\b", re.IGNORECASE)
_OOO_RE = re.compile(
    r"\b(out of (the )?office|automatic reply|auto-reply|away from (my|the) "
    r"(desk|email)|on vacation|currently out of|will be back (on|in))\b",
    re.IGNORECASE)


def classify_reply(subject: str, body: str, headers: dict | None = None) -> str:
    """(subject, body, headers) -> 'unsubscribe' | 'ooo' | 'reply'."""
    headers = headers or {}
    auto_submitted = (headers.get("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return "ooo"
    if headers.get("X-Autoreply") or headers.get("X-Autorespond"):
        return "ooo"

    text = f"{subject}\n{body}"
    if _UNSUB_RE.search(text):
        return "unsubscribe"
    if _OOO_RE.search(text):
        return "ooo"
    return "reply"


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in (part.get("Content-Disposition") or ""):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="ignore")
        return ""
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="ignore") if payload else ""


def fetch_replies(lead_emails: set, since_days: int, timeout_s: float = 25.0) -> list:
    """Messages received since `since_days` ago, from any address in
    `lead_emails`. Returns a list of dicts: from_email, subject, body, headers.

    Runs the actual IMAP work in a daemon thread with a hard EXTERNAL
    deadline. imaplib/ssl don't reliably honor their own timeout= parameter
    on every platform for every phase of a connection (observed: a
    rate-limited/slow-walking server can hang past both the per-socket and
    the global-default timeout) — this guarantees the caller is never
    blocked beyond `timeout_s` regardless of what's stuck underneath. The
    abandoned thread (if any) dies with the process; nothing leaks past exit.
    """
    box = {"value": [], "error": None}

    def worker():
        try:
            box["value"] = _fetch_replies_blocking(lead_emails, since_days)
        except Exception as err:
            box["error"] = err

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        log.warning("IMAP reply check still running after %.0fs — abandoning "
                   "this pass (will try again next run)", timeout_s)
        return []
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


def _fetch_replies_blocking(lead_emails: set, since_days: int) -> list:
    user, password = _credentials()
    since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")

    # Belt-and-suspenders timeout: IMAP4_SSL's own timeout= only covers some
    # phases of the connection (not reliably DNS/TLS handshake on every
    # platform), so a hung/rate-limited server can still block forever
    # without this global socket-level ceiling.
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(20)
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(SMTP["imap_host"], SMTP["imap_port"], timeout=20)
        imap.login(user, password)
        imap.select("INBOX")
        status, data = imap.search(None, f"(SINCE {since_date})")
        if status != "OK":
            log.warning("IMAP search failed: %s", status)
            return []

        results = []
        for msg_id in data[0].split():
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = parseaddr(msg.get("From", ""))[1].lower()
            if from_addr not in lead_emails:
                continue
            results.append({
                "from_email": from_addr,
                "subject": _decode(msg.get("Subject")),
                "body": _extract_body(msg),
                "headers": {
                    "Auto-Submitted": msg.get("Auto-Submitted"),
                    "X-Autoreply": msg.get("X-Autoreply"),
                    "X-Autorespond": msg.get("X-Autorespond"),
                },
            })
        return results
    finally:
        socket.setdefaulttimeout(previous_timeout)
        if imap is not None:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass
