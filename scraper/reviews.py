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
from datetime import date

from core.dates import is_recent, parse_relative
from core.logbook import get_logger
from scraper import review_dom
from scraper.review_dom import STARS_RE

log = get_logger(__name__)


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
    """Turn raw page strings into tidy records, dropping junk.

    Review text is user-generated content — the most hostile input in the
    pipeline — so control characters are stripped here, at the boundary, and a
    generous ceiling guards against a pathological entry. That ceiling is a
    safety bound, NOT formatting: quotes are stored whole, because the email
    quotes them and a sentence ending in "gave us a very w" is worthless. The
    budget that keeps token costs down lives at the prompt instead."""
    cleaned = []
    for r in raw_reviews:
        text = (r.get("text") or "").strip()
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)      # control chars
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 15:                                 # "Great!" helps nobody
            continue
        text = text[:REVIEWS["max_chars_each"]]

        stars_match = STARS_RE.search(r.get("stars") or "")
        when = (r.get("when") or "").strip() or None
        posted = parse_relative(when)
        cleaned.append({
            "stars": float(stars_match.group(1)) if stars_match else None,
            "text": text,
            "when": when,
            "date": posted.isoformat() if posted else None,
            "owner_replied": bool(r.get("owner_replied")),
        })
    return cleaned


def _take(reviews: list, keep, limit: int) -> list:
    """The first `limit` reviews satisfying `keep`, in the order given."""
    picked = []
    for review in reviews:
        if len(picked) >= limit:
            break
        if keep(review):
            picked.append(review)
    return picked


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


def _complaints(page, business_name: str) -> list:
    """Reviews rated at or below REVIEWS['negative_max_stars'].

    Worth the extra sort: a business's complaints are the only part of its
    reviews you can build a pitch on. Praise proves they're good at the work,
    which is necessary but sells nothing."""
    if not REVIEWS["include_lowest_rated"]:
        return []
    if not review_dom.sort_lowest(page):
        # Indistinguishable from here: a business with genuinely no reviews
        # under 4 stars looks exactly like a sort that didn't land. The lead is
        # unpitchable either way, so don't claim a cause we can't prove.
        log.info("  %s: no reviews under %d stars available",
                 business_name, REVIEWS["positive_min_stars"])
        return []
    if REVIEWS["expand_more"]:
        review_dom.expand_more(page)
    found = _clean(review_dom.read_cards(page, REVIEWS["max_per_lead"]))
    return _take(found,
                 lambda r: r["stars"] and r["stars"] <= REVIEWS["negative_max_stars"],
                 REVIEWS["negative_target"])


def _recent(page, business_name: str) -> list:
    """Reviews sorted newest-first, keeping only those inside the capture
    window.

    This exists because the lowest-rating pass finds a business's worst reviews
    EVER, and those accumulate over years — on a real Manchester run six
    businesses had complaints and not one was recent enough for the gate to
    quote. Maps offers no date filter, only this sort, so "recent" means sort
    newest and stop at the cutoff. Complaints found here are fresh by
    construction."""
    if not REVIEWS["include_newest"]:
        return []
    if not review_dom.sort_newest(page):
        log.debug("%s: couldn't sort to newest", business_name)
        return []
    if REVIEWS["expand_more"]:
        review_dom.expand_more(page)
    fresh = []
    for review in _clean(review_dom.read_cards(page, REVIEWS["max_per_lead"])):
        posted = review.get("date")
        if posted and not is_recent(date.fromisoformat(posted),
                                    REVIEWS["max_age_months"]):
            break                   # newest-first: everything after is older
        fresh.append(review)
    return fresh


def collect_reviews(page, business_name: str = "") -> list:
    """Open the reviews tab on an already-loaded listing and return cleaned
    reviews — complaints first, then praise. Returns [] on any failure: never
    raises, never costs the caller its lead.

    Both halves are deliberate. The complaints are what you pitch against; the
    praise is what stops the email reading like an accusation, and it has to be
    specific enough to prove somebody actually looked."""
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

    praise = _take(_clean(relevant),
                   lambda r: r["stars"] and r["stars"] >= REVIEWS["positive_min_stars"],
                   REVIEWS["positive_target"])
    worst = _complaints(page, business_name)
    fresh = _recent(page, business_name)
    fresh_complaints = _take(
        fresh, lambda r: r["stars"] and r["stars"] <= REVIEWS["negative_max_stars"],
        REVIEWS["negative_target"])

    # Recent complaints lead: they're the only ones the gate will quote, and
    # the merge order decides what survives the prompt budget downstream.
    complaints = _merge(fresh_complaints, worst)
    reviews = _merge(complaints, praise)[:REVIEWS["max_per_lead"]]
    _log_capture(business_name, reviews, complaints, praise, len(fresh_complaints))
    return reviews


def _log_capture(business_name, reviews, complaints, praise, fresh=0) -> None:
    """Say what we got and what we're short of. A lead with no complaints can't
    be pitched, and that has to be visible rather than looking like success."""
    truncated = sum(1 for r in reviews if r["text"].rstrip().endswith("…"))
    if truncated:
        log.warning("%s: %d/%d review(s) still truncated — 'More' didn't expand",
                    business_name, truncated, len(reviews))
    if not complaints:
        log.info("  %s: no complaints found — nothing to pitch against",
                 business_name)
    elif not fresh:
        log.info("  %s: %d complaint(s), none inside the last %d months — the "
                 "gate can't quote stale evidence", business_name,
                 len(complaints), REVIEWS["max_age_months"])
    elif len(complaints) < REVIEWS["negative_target"]:
        log.info("  %s: only %d/%d complaint(s) available", business_name,
                 len(complaints), REVIEWS["negative_target"])
    if len(praise) < REVIEWS["positive_target"]:
        log.debug("%s: %d/%d positive review(s)", business_name, len(praise),
                  REVIEWS["positive_target"])
