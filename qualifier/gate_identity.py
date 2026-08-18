"""
G1 — identity. Runs before anything else looks at a lead, because it removes
whole clusters for the price of one pass.

Three jobs: collapse the same business appearing several times, drop the
national chains, drop the closed. All free, all deterministic.

The deduplication matters more than it sounds. One Austin search returned three
listings sharing a phone number and a domain — two "Compass Real Estate | North
Austin" plus a "Realty Austin Compass". Treat those as three leads and one
person gets three cold emails from you.
"""

import re
from urllib.parse import urlparse

from core.logbook import get_logger

log = get_logger(__name__)

CLOSED_STATES = {"Permanently closed", "Temporarily closed"}

# Domains too common to identify a business by.
_SHARED_HOSTS = {"facebook.com", "instagram.com", "linktr.ee", "google.com",
                 "business.site", "wixsite.com", "sites.google.com"}


def phone_key(phone: str) -> str:
    """Digits only, so '+1 555-010-0100' and '5550100100' match."""
    return re.sub(r"\D", "", phone or "")[-10:]


def domain_key(website: str) -> str:
    """Registrable-ish host, or '' for shared platforms that identify nobody."""
    if not website:
        return ""
    host = (urlparse(website).netloc or "").lower().removeprefix("www.")
    if not host or any(host.endswith(shared) for shared in _SHARED_HOSTS):
        return ""
    return host


def is_chain(name: str, chains: list) -> str | None:
    """The chain this name belongs to, or None. Substring match: franchises
    append a town ('Dyno-Rod Manchester')."""
    lowered = (name or "").lower()
    for chain in chains or []:
        if chain.lower() in lowered:
            return chain
    return None


def check(lead: dict, profile: dict) -> str | None:
    """Drop reason for one lead, or None if it passes."""
    if lead.get("open_state") in CLOSED_STATES:
        return f"closed ({lead['open_state']})"

    chain = is_chain(lead.get("name"), profile.get("chains"))
    if chain:
        return f"chain ({chain})"

    category = (lead.get("category") or "").lower()
    for unwanted in profile.get("categories_exclude") or []:
        if category == unwanted.lower():
            return f"category excluded ({lead.get('category')})"

    # The include list was in every profile and never enforced, so a national
    # boiler company caught by a "solar panel installation" search sailed
    # through to Tier A. A missing category isn't a reason to drop — plenty of
    # scrapes run without it — but a category that's clearly the wrong trade is.
    wanted = profile.get("maps_categories_include") or []
    if category and wanted and not any(category == w.lower() for w in wanted):
        return f"wrong trade for this vertical ({lead.get('category')})"

    if is_national(lead.get("phone"), profile):
        return f"national number, not a local trade ({lead.get('phone')})"
    return None


def is_national(phone: str, profile: dict) -> bool:
    """Is this a non-geographic number?

    An 0333 or 0800 line means a call centre somewhere, not a van in
    Manchester — and you cannot sell phone answering to a business whose whole
    model is answering phones. The name blocklist only catches chains you
    already know about; this catches the ones you don't."""
    digits = re.sub(r"\D", "", phone or "")
    digits = digits[2:] if digits.startswith("44") else digits
    if not digits.startswith("0"):
        digits = "0" + digits
    return any(digits.startswith(p)
               for p in profile.get("national_phone_prefixes") or [])


def duplicates(leads: list) -> dict:
    """{place_key: reason} for every lead that is another lead's second
    listing. Runs over the whole batch — this is the one check that can't be
    made looking at a single row.

    The first listing seen wins, and the rest point at it by name, so the
    spreadsheet shows you what was merged rather than silently losing rows."""
    by_phone, by_domain, dupes = {}, {}, {}
    for lead in leads:
        key = lead.get("place_key")
        phone, domain = phone_key(lead.get("phone")), domain_key(lead.get("website"))
        original = (by_phone.get(phone) if phone else None) or \
                   (by_domain.get(domain) if domain else None)
        if original:
            # Branches often share a name, so say where the original was or the
            # line reads like "duplicate of itself".
            dupes[key] = f"duplicate of {original[0]} ({original[1] or 'no address'})"
            continue
        identity = (lead.get("name"), lead.get("address"))
        if phone:
            by_phone[phone] = identity
        if domain:
            by_domain[domain] = identity
    if dupes:
        log.info("G1: %d duplicate listing(s) collapsed", len(dupes))
    return dupes
