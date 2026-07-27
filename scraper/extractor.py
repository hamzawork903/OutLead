"""
Opens one listing at a time and pulls the lead fields. Built on Maps'
`data-item-id` anchors (address / authority / phone), which are the most
stable part of the DOM — verified live against the new UI (2026-07).

Design rules:
  - A listing that won't open is SKIPPED (logged), never a crash — one bad
    page can't sink a 120-listing run.
  - A missing website/phone/rating is a FACT about the business, not an error
    (plenty of real businesses have no website). Logged at debug only.
"""

import re

from config import TIMEOUTS_MS
from core import humanizer
from core.browser import save_failure_screenshot
from core.logbook import get_logger
from core.models import Lead
from core.reliability import BlockDetected, TRANSIENT, guard_block, retry

log = get_logger(__name__)

# The header rating blob looks like "5.0(104)" or "4.9(1,306)".
_RATING_RE = re.compile(r"([0-9]+(?:\.[0-9])?)\s*\(([\d,]+)\)")

# One pass in the page: pull every raw field we need, then clean in Python.
_EXTRACT_JS = r"""() => {
    const q = (sel) => document.querySelector(sel);
    const t = (el) => el ? el.textContent.trim() : null;
    const site  = q('a[data-item-id="authority"]');
    const phone = q('button[data-item-id^="phone"]');
    const addr  = q('button[data-item-id="address"]');
    const oloc  = q('button[data-item-id="oloc"]');

    // open/closed status: shortest element text starting with a status word
    let status = null;
    const cands = [...document.querySelectorAll('span, div')]
        .map(e => e.textContent.trim())
        .filter(x => /^(Open|Closed|Open 24 hours|Permanently closed|Temporarily closed)\b/.test(x) && x.length < 70);
    if (cands.length) { cands.sort((a, b) => a.length - b.length); status = cands[0]; }

    // weekly hours from per-day aria-labels ("Monday, 8 AM to 5 PM, ...")
    const seen = {};
    const days = [];
    for (const el of document.querySelectorAll('[aria-label]')) {
        const a = el.getAttribute('aria-label') || '';
        const m = a.match(/^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*(.+?)(?:,\s*(?:Copy|Hours|Suggest)|$)/);
        if (m && !seen[m[1]]) { seen[m[1]] = 1; days.push(m[1].slice(0,3) + ' ' + m[2].trim()); }
    }

    // price level: aria "Price: ..." or a $-run span
    let price = null;
    const pa = q('[aria-label^="Price:"], [aria-label*="Price:"]');
    if (pa) price = (pa.getAttribute('aria-label').split('Price:')[1] || '').trim();
    if (!price) {
        const ds = [...document.querySelectorAll('span')].map(s => s.textContent.trim())
            .find(x => /^\${1,4}$/.test(x) || /^\$\d/.test(x) || /^\$\S+\s*[–-]\s*\$\S+$/.test(x));
        if (ds) price = ds;
    }

    return {
        name: t(q('h1')),
        category: t(q('button[jsaction*="category"]')) || t(q('.DkEaL')),
        rating_blob: t(q('div.F7nice')),
        address: addr ? t(addr.querySelector('.Io6YTe')) : null,
        website: site ? site.href : null,
        phone_id: phone ? (phone.getAttribute('data-item-id') || '') : null,
        phone_display: phone ? t(phone.querySelector('.Io6YTe')) : null,
        plus_code: oloc ? (t(oloc.querySelector('.Io6YTe')) || null) : null,
        status: status,
        hours: days.join('; ') || null,
        price: price || null,
    };
}"""

_PLUS_CODE_RE = re.compile(r"[A-Z0-9]{4,}\+[A-Z0-9]{2,}")


def _parse_open_state(status: str | None) -> str | None:
    if not status:
        return None
    s = status.lower()
    if s.startswith("permanently closed"):
        return "Permanently closed"
    if s.startswith("temporarily closed"):
        return "Temporarily closed"
    if s.startswith("open 24 hours"):
        return "Open 24 hours"
    if s.startswith("open"):
        return "Open"
    if s.startswith("closed"):
        return "Closed"
    return None


def _clean_plus_code(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _PLUS_CODE_RE.search(raw)
    return m.group(0) if m else None


def _clean_ws(s: str | None) -> str | None:
    """Normalize Google's narrow/no-break spaces to plain spaces (keeps CSVs
    and hours strings readable)."""
    if not s:
        return None
    return s.replace(" ", " ").replace("\xa0", " ").strip() or None


def extract_lead(page, listing, query: str) -> Lead | None:
    """
    Navigate to a listing and return a Lead.

    Returns None if the listing won't open (already retried) — the run skips it
    and continues. Raises BlockDetected if Google challenges us, so the caller
    can stop the whole run cleanly (progress stays saved).
    """
    place_key = listing.place_key

    def _open():
        page.goto(listing.url, timeout=TIMEOUTS_MS["page_load"],
                  wait_until="domcontentloaded")
        page.wait_for_selector("h1", timeout=TIMEOUTS_MS["element_wait"])

    try:
        retry(_open, label=f"open {listing.name!r}")
    except BlockDetected:
        raise
    except TRANSIENT:
        shot = save_failure_screenshot(page, "extract-timeout")
        log.warning("Couldn't open listing %r after retries — skipping it "
                    "(screenshot: %s)", listing.name, shot)
        return None

    guard_block(page)  # opened page might itself be a challenge -> stop the run
    humanizer.pause("reading_pause")  # let the panel finish hydrating
    raw = page.evaluate(_EXTRACT_JS)

    rating, reviews = _parse_rating(raw.get("rating_blob"))
    phone = raw.get("phone_display") or _clean_phone(raw.get("phone_id"))
    lead = Lead(
        place_key=place_key,
        name=raw.get("name") or listing.name,
        category=raw.get("category"),
        address=_clean_ws(raw.get("address")),
        phone=_clean_ws(phone),
        website=raw.get("website"),
        rating=rating,
        reviews=reviews,
        maps_url=listing.url,
        query=query,
        open_state=_parse_open_state(raw.get("status")),
        hours=_clean_ws(raw.get("hours")),
        price_level=_clean_ws(raw.get("price")),
        plus_code=_clean_plus_code(raw.get("plus_code")),
    )

    if not lead.name:
        log.warning("listing %s had no name — data may be incomplete", place_key)
    absent = [f for f in ("category", "address", "phone", "website", "rating")
              if getattr(lead, f) in (None, "")]
    if absent:
        log.debug("%s: no %s (normal for some businesses)",
                  lead.name, ", ".join(absent))
    log.debug("extracted %r | phone=%s website=%s rating=%s(%s)",
              lead.name, lead.phone, bool(lead.website), lead.rating, lead.reviews)
    return lead


def lead_passes(lead, *, min_rating=None, require_website=False,
                skip_closed=False):
    """Decide whether a fully-extracted lead should be kept. Returns
    (keep: bool, reason: str|None). Applied after extraction, so it trims the
    database and the (expensive) enrichment pass rather than the page load."""
    if min_rating is not None:
        if lead.rating is None or lead.rating < min_rating:
            return False, f"rating {lead.rating} < {min_rating}"
    if require_website and not lead.website:
        return False, "no website"
    if skip_closed and lead.open_state in ("Permanently closed", "Temporarily closed"):
        return False, lead.open_state.lower()
    return True, None


def _parse_rating(blob: str | None) -> tuple[float | None, int | None]:
    if not blob:
        return None, None
    match = _RATING_RE.search(blob.replace(" ", " "))
    if not match:
        return None, None
    return float(match.group(1)), int(match.group(2).replace(",", ""))


def _clean_phone(item_id: str | None) -> str | None:
    if not item_id:
        return None
    return item_id.replace("phone:tel:", "").strip() or None
