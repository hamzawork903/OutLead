"""
The site harvester — the network half of enrichment. Given one business
website, it hunts for an email while staying polite and crash-proof:

  - homepage first; only if empty, follow a few contact-ish pages
  - hard per-request timeout AND a total per-site time budget
  - page-size cap (never download a 40 MB page)
  - only text/html, only the business's own domain

Returns an EnrichResult the caller streams straight to the DB + CSV. Never
raises for a bad site — a dead/hanging site becomes status "site_dead".
"""

import re
import time
from dataclasses import dataclass, field

import httpx

from config import ENRICH, LLM
from core.logbook import get_logger
from enrichment import email_finder as ef

log = get_logger(__name__)


@dataclass
class EnrichResult:
    place_key: str
    email: str | None
    all_emails: list = field(default_factory=list)
    source: str | None = None       # which page the email came from
    status: str = "no_email"        # enriched | no_email | site_dead
    socials: dict = field(default_factory=dict)   # platform -> profile URL
    phones: list = field(default_factory=list)    # phone numbers on the website
    page_text: str = ""             # homepage visible text (LLM personalization)


_TAG_STRIP_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>",
                           re.IGNORECASE | re.DOTALL)
_TAGS_RE = re.compile(r"<[^>]+>")


def _text_of(html: str, cap: int) -> str:
    """Visible text of a page: drop script/style, strip tags, tidy whitespace."""
    text = _TAG_STRIP_RE.sub(" ", html)
    text = _TAGS_RE.sub(" ", text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:cap]


def _fetch(client, url):
    """Return (final_url, html_text) or None. Enforces content-type + size."""
    try:
        resp = client.get(url, timeout=ENRICH["page_timeout_s"],
                          follow_redirects=True,
                          headers={"User-Agent": ENRICH["user_agent"]})
    except Exception as err:                       # DNS, TLS, timeout, refused
        log.debug("fetch failed %s (%s)", url, type(err).__name__)
        return None

    if resp.status_code >= 400:                    # 403 bot-block, 404, 5xx...
        log.debug("fetch %s -> HTTP %d", url, resp.status_code)
        return None

    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        log.debug("skip non-html %s (%s)", url, ctype or "no content-type")
        return None

    raw = resp.content[:ENRICH["max_bytes"]]
    return str(resp.url), raw.decode(resp.encoding or "utf-8", errors="ignore")


def harvest_site(place_key: str, website: str) -> EnrichResult:
    """Find the best email for one business website."""
    started = time.time()
    site_domain = ef.domain_of(website)

    socials: dict = {}
    phones: dict = {}                              # digits -> display (dedupe)

    def _absorb(html, url):
        for platform, link in ef.social_links_from_html(html, url).items():
            socials.setdefault(platform, link)     # first find per platform wins
        for ph in ef.phones_from_html(html):
            phones.setdefault(re.sub(r"\D", "", ph), ph)

    with httpx.Client() as client:
        first = _fetch(client, website)
        if first is None:
            log.debug("site_dead: %s", website)
            return EnrichResult(place_key, None, status="site_dead")

        final_url, home_html = first
        emails = ef.emails_from_html(home_html)
        _absorb(home_html, final_url)              # contacts usually in the footer
        source = "homepage" if emails else None
        page_text = _text_of(home_html, LLM["max_site_chars"])

        # Visit contact-ish pages only if we still lack an email or found no
        # socials at all (they're usually in the homepage footer already).
        if (not emails) or (not socials):
            links = ef.contact_links(home_html, final_url, ENRICH["contact_hints"])
            for link in links[: ENRICH["max_pages"] - 1]:
                if time.time() - started > ENRICH["site_budget_s"]:
                    log.debug("site budget hit for %s", website)
                    break
                page = _fetch(client, link)
                if not page:
                    continue
                _absorb(page[1], link)
                if not emails:
                    hits = ef.emails_from_html(page[1])
                    if hits:
                        emails, source = hits, link
                        break                      # have an email; stop crawling

    phone_list = list(phones.values())
    best, ranked = ef.pick_best(emails, site_domain)
    if best:
        log.debug("found %s for %s (via %s) | %d socials | %d phones",
                  best, website, source, len(socials), len(phone_list))
        return EnrichResult(place_key, best, ranked, source, "enriched",
                            socials, phone_list, page_text)
    log.debug("no email for %s | %d socials | %d phones",
              website, len(socials), len(phone_list))
    return EnrichResult(place_key, None, [], None, "no_email", socials,
                        phone_list, page_text)
