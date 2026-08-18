"""
G3 — can we actually reach them, and is this address really theirs?

The second half matters more than it looks. Scraping a homepage for an email
finds plenty of addresses that belong to somebody else entirely:

  bugreport@moatable.com          a website vendor's bug inbox, on two firms
  ...@webform.boxly.ai            a form relay, shared by four businesses
  info@indiantypefoundry.com      a font foundry, scraped off a pizzeria

All three passed MX checks and were stored as valid. The tell isn't the address
— it's that one domain turns up across unrelated businesses. A real company
email appears on exactly one company.
"""

from collections import defaultdict

from core.logbook import get_logger

log = get_logger(__name__)

# Hosts that legitimately appear on many unrelated businesses. Free mailboxes
# are normal for a small trade, and nhs.net is the health service, not a vendor
# — auto-blocking either would quietly delete good leads.
_CONSUMER_HOSTS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                   "yahoo.com", "yahoo.co.uk", "live.co.uk", "icloud.com",
                   "btinternet.com", "sky.com", "aol.com", "me.com",
                   "nhs.net", "nhs.uk"}

DEAD_STATUSES = {"invalid", "undeliverable", "disposable"}


def _domain(email: str) -> str:
    return (email or "").split("@")[-1].strip().lower()


def suspect_domains(leads: list, min_businesses: int = 3) -> set:
    """Domains worth a human look: one address turning up across unrelated
    businesses usually means a vendor or a form relay rather than a company.

    ADVISORY ONLY — this reports, it never drops. Run against the real
    database it flagged moatable.com and indiantypefoundry.com correctly, and
    also nhs.net and compass.com, which are perfectly real. There is no
    reliable way to tell "website vendor" from "large legitimate employer" by
    counting, so the machine suggests and `vendor_email_domains` in the profile
    decides. Chains are a separate problem with a separate filter."""
    by_email_domain = defaultdict(set)
    for lead in leads:
        host = _domain(lead.get("email"))
        if not host or host in _CONSUMER_HOSTS:
            continue
        site = (lead.get("website") or "").lower()
        site_host = site.split("//")[-1].split("/")[0].removeprefix("www.")
        by_email_domain[host].add(site_host or lead.get("name") or "")

    suspects = {host for host, sites in by_email_domain.items()
                if len(sites) >= min_businesses and host not in sites}
    if suspects:
        log.info("G3: %d domain(s) look like vendors — review and add to "
                 "vendor_email_domains if so: %s",
                 len(suspects), ", ".join(sorted(suspects)))
    return suspects


def check(lead: dict, profile: dict, suppressed: set = frozenset()) -> str | None:
    """Drop reason, or None. `suppressed` is the real suppression list from the
    database — the sheet's do_not_contact column feeds into it, never past it."""
    email = (lead.get("email") or "").strip().lower()
    if not email:
        return "no email found"

    status = (lead.get("email_status") or "").lower()
    if status in DEAD_STATUSES:
        return f"email {status}"

    host = _domain(email)
    for domain in profile.get("vendor_email_domains") or []:
        if host == domain.lower() or host.endswith("." + domain.lower()):
            return f"vendor/relay address ({host})"

    if email in suppressed:
        return "on the suppression list"

    already = {a.strip().lower() for a in profile.get("already_contacted") or []}
    if email in already:
        return "already contacted"
    return None
