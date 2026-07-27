"""
The mailer — sends one email over SMTP (Gmail by default; any SMTP/STARTTLS
provider works by changing config.SMTP). Credentials come from environment
variables only, never from a file that could be committed:

    SMTP_EMAIL          the sending address
    SMTP_APP_PASSWORD   a Gmail "App Password" (not your normal password)

Dry-run is a HARD gate: when dry_run=True, this module never opens a network
connection — it only logs what it would have sent. Real sending requires
config.SEQUENCE["dry_run"] = False (or --live on the CLI) AND both env vars set.
"""

import os
import random
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from config import SMTP
from core.logbook import get_logger

log = get_logger(__name__)

# Same shape as core/reliability.py's taxonomy, scoped to SMTP: transient
# (connection hiccups) get retried with backoff; permanent (bad credentials,
# address rejected) never do — retrying those just wastes time and looks
# suspicious to the mail server.
_TRANSIENT = (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
             smtplib.SMTPHeloError, TimeoutError, OSError)
_PERMANENT = (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
             smtplib.SMTPSenderRefused, smtplib.SMTPDataError)


class SendResult:
    def __init__(self, status: str, error: str | None = None):
        # "sent" | "dry_run" | "failed" (the address is bad — stop the
        # sequence) | "retry_later" (WE couldn't reach the mail server —
        # keep the lead and try again next pass)
        self.status = status
        self.error = error


def _credentials() -> tuple[str, str]:
    email = os.environ.get("SMTP_EMAIL")
    password = os.environ.get("SMTP_APP_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "SMTP_EMAIL and SMTP_APP_PASSWORD must be set as environment "
            "variables to send real email. See config.py's SMTP comment for "
            "how to create a Gmail App Password."
        )
    return email, password


def _build_message(from_email: str, to_email: str, subject: str, body: str,
                   html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Reply-To"] = from_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)                          # plain text is always there
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(to_email: str, subject: str, body: str, *, dry_run: bool,
              html_body: str | None = None, attempts: int = 3) -> SendResult:
    """Send one email (plain text + optional HTML alternative). In dry-run
    mode, nothing touches the network."""
    if dry_run:
        log.info("  [dry-run] would send to %s | subject: %s", to_email, subject)
        log.debug("[dry-run] body:\n%s", body)
        return SendResult("dry_run")

    from_email, password = _credentials()
    msg = _build_message(from_email, to_email, subject, body, html_body)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with smtplib.SMTP(SMTP["host"], SMTP["port"], timeout=20) as server:
                server.starttls()
                server.login(from_email, password)
                server.send_message(msg)
            log.info("  sent to %s | subject: %s", to_email, subject)
            return SendResult("sent")
        except _PERMANENT as err:
            log.error("send to %s permanently failed (%s): %s",
                     to_email, type(err).__name__, err)
            return SendResult("failed", f"{type(err).__name__}: {err}")
        except _TRANSIENT as err:
            last_error = err
            if attempt >= attempts:
                break
            delay = min(20.0, 2.0 * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
            log.warning("send to %s failed (%s) — retry %d/%d in ~%.0fs",
                       to_email, type(err).__name__, attempt + 1, attempts, delay)
            time.sleep(delay)

    # Every attempt hit a TRANSIENT error — that's our network/the server,
    # not a bad address. Report it as retry_later so the caller keeps the
    # lead alive instead of burning it.
    log.error("send to %s exhausted all retries (%s) — will retry next pass",
              to_email, last_error)
    return SendResult("retry_later", f"{type(last_error).__name__}: {last_error}")
