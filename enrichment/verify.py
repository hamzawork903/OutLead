"""
Email verification — free, safe, and honest about its limits.

Two checks, no spammy SMTP probing (which is unreliable and can get your IP
blacklisted):
  1. Syntax — is it a well-formed address?
  2. MX / A record — does the domain actually accept mail? A domain with no
     mail servers can NEVER receive email, so any address there is dead.

Verdicts:
  valid    — good syntax AND the domain has mail servers
  risky    — good syntax but no mail servers found (likely undeliverable)
  invalid  — malformed address
  unknown  — DNS couldn't be checked (no dnspython / lookup error)

MX results are cached per domain, so a list of leads sharing a domain costs one
lookup. Thread-safe so it can be called from the enrichment workers too.
"""

import os
import re
import threading

import httpx

from config import MAILBOX_VERIFY
from core.logbook import get_logger

log = get_logger(__name__)

try:
    import dns.resolver
    _HAVE_DNS = True
except ImportError:                       # pragma: no cover
    _HAVE_DNS = False
    log.warning("dnspython not installed — email MX checks disabled")

_SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")
_DNS_TIMEOUT = 5.0

# Template placeholders scraped off unfinished websites. These pass syntax and
# often even MX (website.com is a real domain!) but no human reads them — one
# already burned a real send (youremail@website.com). Conservative on purpose:
# 'info'/'contact' locals are the most common REAL business addresses, so only
# unambiguous template locals and known filler domains are listed.
_PLACEHOLDER_LOCALS = {
    "youremail", "your.email", "your-email", "yourname", "your.name", "your",
    "example", "sample", "demo", "test", "testing", "placeholder",
    "username", "user", "someone", "firstname", "lastname",
    "firstname.lastname", "john.doe", "johndoe", "jane.doe",
}
_PLACEHOLDER_DOMAINS_RE = re.compile(
    r"^(example\.(com|org|net)|website\.com|yourdomain\.[a-z]+|yoursite\.[a-z]+|"
    r"domain\.com|mydomain\.com|mysite\.com|emailaddress\.com|placeholder\.[a-z]+|"
    r"sentry\.io|.*\.wixpress\.com)$")


def is_placeholder(email: str) -> bool:
    """True for template filler like youremail@website.com — never a real inbox."""
    local, _, domain = email.lower().partition("@")
    return local in _PLACEHOLDER_LOCALS or bool(_PLACEHOLDER_DOMAINS_RE.match(domain))

_mx_cache: dict[str, bool | None] = {}
_lock = threading.Lock()


def _domain_accepts_mail(domain: str) -> bool | None:
    """True if the domain has MX (or a fallback A) records; None if unknowable."""
    with _lock:
        if domain in _mx_cache:
            return _mx_cache[domain]

    result: bool | None
    if not _HAVE_DNS:
        result = None
    else:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=_DNS_TIMEOUT)
            result = len(answers) > 0
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # No MX — some domains still accept mail via an implicit A record.
            try:
                dns.resolver.resolve(domain, "A", lifetime=_DNS_TIMEOUT)
                result = True
            except Exception:
                result = False
        except Exception as err:           # timeout, no nameservers, etc.
            log.debug("MX lookup failed for %s (%s)", domain, type(err).__name__)
            result = None

    with _lock:
        _mx_cache[domain] = result
    return result


def verify_email(email: str | None) -> str:
    """Return 'valid' | 'risky' | 'invalid' | 'unknown' for a single address."""
    if not email or not _SYNTAX_RE.match(email):
        return "invalid"
    if is_placeholder(email):
        return "invalid"
    domain = email.rsplit("@", 1)[1].lower()
    accepts = _domain_accepts_mail(domain)
    if accepts is None:
        return "unknown"
    return "valid" if accepts else "risky"


# ---------------------------------------------- mailbox check (Reoon) ----
# POWER mode asks the recipient's mail server whether the individual inbox
# exists — the step MX records can't answer. Free tier; key in .env.

_REOON_URL = "https://emailverifier.reoon.com/api/v1/verify"

# Reoon status -> our email_status. Missing keys (e.g. 'unknown') mean
# "no verdict": keep the MX-based status we already had.
_REOON_MAP = {
    "safe": "valid",
    "role_account": "valid",       # info@/office@ ARE the standard B2B contact
    "invalid": "invalid",
    "disabled": "invalid",
    "disposable": "invalid",
    "spamtrap": "invalid",         # sending to these poisons the sender score
    "inbox_full": "risky",
    "catch_all": "valid" if MAILBOX_VERIFY["catch_all_ok"] else "risky",
}

_warned_no_reoon_key = False


def mailbox_status(email: str) -> str | None:
    """Ask Reoon whether this inbox exists. Returns our mapped status, or
    None when there's no verdict (no key, disabled, quota out, API error) —
    callers then keep the MX-based verdict."""
    global _warned_no_reoon_key
    key = os.environ.get("REOON_API_KEY")
    if not MAILBOX_VERIFY["enabled"] or not key:
        if not key and MAILBOX_VERIFY["enabled"] and not _warned_no_reoon_key:
            _warned_no_reoon_key = True
            log.info("  mailbox verification off: REOON_API_KEY not set in .env")
        return None
    try:
        resp = httpx.get(_REOON_URL, timeout=MAILBOX_VERIFY["timeout_s"],
                         params={"email": email, "key": key,
                                 "mode": MAILBOX_VERIFY["mode"]})
        if resp.status_code != 200:
            log.warning("mailbox check failed for %s (HTTP %d) — keeping "
                        "MX-based verdict", email, resp.status_code)
            return None
        status = (resp.json().get("status") or "").lower()
    except (httpx.HTTPError, ValueError) as err:
        log.warning("mailbox check failed for %s (%s) — keeping MX-based "
                    "verdict", email, type(err).__name__)
        return None
    mapped = _REOON_MAP.get(status)
    log.debug("mailbox check %s -> %s (mapped: %s)", email, status, mapped)
    return mapped


def best_verified(ranked_emails):
    """From ranked candidates, return (email, status) preferring a deliverable
    one. An address only comes back 'valid' if it passed syntax + MX + the
    placeholder rules AND (when Reoon is configured) the inbox actually
    exists; proven-dead candidates are skipped in favor of the next one."""
    if not ranked_emails:
        return None, None
    fallback = None
    for email in ranked_emails:
        status = verify_email(email)
        if status == "valid":
            deep = mailbox_status(email)
            if deep == "invalid":
                log.info("  %s looks fine but the inbox doesn't exist — "
                         "trying next candidate", email)
                if fallback is None:
                    fallback = (email, "invalid")
                continue
            if deep is not None:
                status = deep
            if status == "valid":
                return email, "valid"
        if fallback is None:
            fallback = (email, status)
    return fallback
