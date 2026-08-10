"""
Review capture — the one field that needs its own interaction.

Everything else in a listing sits in the panel Maps already rendered, so the
extractor reads it in a single evaluate(). Reviews are behind a tab click, a
lazy-loading scroll panel and a sort menu, which makes this the slowest and
most fragile field. Driving that panel lives in review_dom.py; this file
decides what we ask for and what we keep.

Two policies worth knowing:

  * A read is never allowed to go backwards. We read what's on screen first
    and only then scroll for more, keeping whichever read was best — so a
    scroll that upsets the panel can fail to add but can never take away.

  * We deliberately go and fetch the worst reviews (see review_dom.sort_lowest).
    Praise is pleasant; a customer complaining they can't get anyone on the
    phone is the reason this feature exists.

It fails soft — a lead with no reviews is still a good lead — but never
silently: a card we can see and can't read is logged as a warning, because a
quiet [] once hid a bug that lost reviews on 41% of leads.
"""

import re

from config import REVIEWS, TIMEOUTS_MS
from core.logbook import get_logger
from scraper import review_dom

log = get_logger(__name__)

_STARS_RE = re.compile(r"([0-9](?:\.[0-9])?)\s*star", re.IGNORECASE)


def _readable(raw_reviews: list) -> int:
    """How many raw cards actually carried text — the measure of a good read."""
    return sum(1 for r in raw_reviews if (r.get("text") or "").strip())


def _merge(first: list, second: list) -> list:
    """Both lists, first one's order preserved, duplicates dropped. Order is
    load-bearing: the char budget in _clean truncates the tail, so whatever
    matters most has to be passed in first."""
    merged, seen = [], set()
    for review in [*first, *second]:
        key = (review.get("text") or "").strip()[:80].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(review)
    return merged


def _clean(raw_reviews: list) -> list:
    """Turn raw page strings into tidy records, dropping junk. Review text is
    user-generated content — the most hostile input in the pipeline — so it's
    length-capped and stripped of control characters here, at the boundary."""
    cleaned, total = [], 0
    for r in raw_reviews:
        text = (r.get("text") or "").strip()
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)      # control chars
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 15:                                 # "Great!" helps nobody
            continue
        text = text[:REVIEWS["max_chars_each"]]
        if total + len(text) > REVIEWS["max_chars_total"]:
            break
        total += len(text)

        stars_match = _STARS_RE.search(r.get("stars") or "")
        cleaned.append({
            "stars": float(stars_match.group(1)) if stars_match else None,
            "text": text,
            "when": (r.get("when") or "").strip() or None,
            "owner_replied": bool(r.get("owner_replied")),
        })
    return cleaned


def _open_panel(page, business_name: str) -> bool:
    """Click through to the reviews list. False (logged) if it won't open."""
    tab = review_dom.find_tab(page)
    if tab is None:
        log.info("  %s: no reviews tab — skipping reviews", business_name)
        return False
    try:
        tab.click(timeout=TIMEOUTS_MS["click"])
        page.wait_for_selector("div[data-review-id]",
                               timeout=REVIEWS["panel_timeout_ms"])
        return True
    except Exception as err:
        log.info("  %s: reviews panel didn't open (%s)", business_name,
                 type(err).__name__)
        return False


def _best_read(page) -> list:
    """Read the panel, scrolling for more, keeping the best read we managed."""
    best = []
    for _ in range(REVIEWS["scroll_rounds"] + 1):
        if REVIEWS["expand_more"]:
            review_dom.expand_more(page)
        raw = review_dom.read_cards(page, REVIEWS["max_per_lead"])
        if _readable(raw) > _readable(best):
            best = raw
        if len(best) >= REVIEWS["max_per_lead"]:
            break
        review_dom.scroll_once(page)
    return best


def _worst_reviews(page) -> list:
    """The lowest-rated reviews, where the complaints live. [] if unavailable."""
    if not REVIEWS["include_lowest_rated"]:
        return []
    if not review_dom.sort_lowest(page):
        return []
    if REVIEWS["expand_more"]:
        review_dom.expand_more(page)
    raw = review_dom.read_cards(page, REVIEWS["max_per_lead"])
    return raw[:REVIEWS["lowest_rated_max"]]


def collect_reviews(page, business_name: str = "") -> list:
    """Open the reviews tab on an already-loaded listing and return up to
    REVIEWS['max_per_lead'] cleaned reviews, complaints first. Returns [] on
    any failure — never raises, never costs the caller its lead."""
    if not _open_panel(page, business_name):
        return []

    relevant = _best_read(page)
    if not relevant:
        log.info("  %s: reviews panel opened but held no cards", business_name)
        return []
    # Cards on screen but nothing readable in them means our selectors have
    # drifted, not that the business has no reviews. Never let that be silent.
    if not _readable(relevant):
        log.warning("%s: %d review card(s) visible but no text extracted — "
                    "the reviews DOM has changed", business_name, len(relevant))
        return []

    reviews = _clean(_merge(_worst_reviews(page), relevant))
    truncated = sum(1 for r in reviews if r["text"].rstrip().endswith("…"))
    if truncated:
        log.warning("%s: %d/%d review(s) still truncated — 'More' didn't expand",
                    business_name, truncated, len(reviews))
    return reviews
