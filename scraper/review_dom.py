"""
Drives Google's review panel: open the tab, expand, scroll, sort, read.

The selectors and the JavaScript live in review_js.py; the policy — how many
reviews to keep, which count as complaints — lives in reviews.py. This file is
only the hands.

Every function fails soft, and the ones that can fail invisibly check their own
outcome rather than trusting that a click landed. Two bugs here were silent for
a whole scrape: a sort that never applied, and an expand that never fired.
"""

import re

from config import REVIEWS
from core.logbook import get_logger
from scraper.review_js import (EXPAND_JS, EXPAND_TRUNCATED_JS, OPEN_SORT_JS,
                               PICK_LOWEST_JS, REVIEWS_JS, SCROLL_JS,
                               SCROLL_TOP_JS, TAB_SELECTORS, TOP_STARS_JS)

log = get_logger(__name__)

# Stars come from the card's accessibility label ("5 stars"), the one part of
# a review card Google has never renamed. Shared with reviews.py.
STARS_RE = re.compile(r"([0-9](?:\.[0-9])?)\s*star", re.IGNORECASE)



def find_tab(page):
    """The Reviews tab, or None if this listing genuinely hasn't got one.
    Retries briefly: the tab strip hydrates a moment after the h1, so a single
    look loses reviews on whichever listings happen to render slowly."""
    for attempt in range(REVIEWS["tab_attempts"]):
        for sel in TAB_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    return el
            except Exception:                   # selector unsupported / detached
                continue
        if attempt + 1 < REVIEWS["tab_attempts"]:
            page.wait_for_timeout(REVIEWS["tab_wait_ms"])
    return None


def read_cards(page, limit: int) -> list:
    """Raw review dicts for the cards currently rendered. [] if unreadable."""
    try:
        return page.evaluate(REVIEWS_JS, limit) or []
    except Exception as err:
        log.warning("review extraction failed (%s)", type(err).__name__)
        return []


def expand_more(page) -> int:
    """Click the 'More' controls so we capture full review text. Expanding
    re-renders the list, so newly-shown cards need another pass; returns how
    many were clicked. Best-effort: any failure just means shorter text."""
    clicked = 0
    for _ in range(REVIEWS["expand_passes"]):
        try:
            found = page.evaluate(EXPAND_JS)
        except Exception as err:
            log.debug("review expand pass failed (%s)", type(err).__name__)
            break
        clicked += found
        if not found:
            break
        page.wait_for_timeout(REVIEWS["expand_wait_ms"])

    try:                                    # anything still visibly cut off
        if page.evaluate(EXPAND_TRUNCATED_JS):
            page.wait_for_timeout(REVIEWS["expand_wait_ms"])
    except Exception as err:
        log.debug("truncated-text expand failed (%s)", type(err).__name__)
    return clicked


def scroll_once(page) -> None:
    """Scroll the reviews list one screen to pull in the next lazy batch."""
    try:
        page.evaluate(SCROLL_JS)
        page.wait_for_timeout(REVIEWS["scroll_wait_ms"])
    except Exception as err:
        log.debug("review scroll failed (%s)", type(err).__name__)


def top_stars(page, count: int) -> list:
    """Ratings of the first `count` cards, for checking a sort really applied."""
    stars = []
    try:
        for label in page.evaluate(TOP_STARS_JS, count) or []:
            found = STARS_RE.search(label or "")
            if found:
                stars.append(float(found.group(1)))
    except Exception as err:
        log.debug("reading top stars failed (%s)", type(err).__name__)
    return stars


def _click_sort_lowest(page) -> None:
    """Drive the sort menu to 'Lowest rating'. Says nothing about whether it
    worked — _wait_until_sorted is what decides that."""
    page.keyboard.press("Escape")           # clear a menu left open by a retry
    try:                                    # control is at the top; we scrolled away
        page.evaluate(SCROLL_TOP_JS)
        page.wait_for_timeout(REVIEWS["scroll_wait_ms"])
    except Exception as err:
        log.debug("scroll to top failed (%s)", type(err).__name__)
    if not page.evaluate(OPEN_SORT_JS):
        return
    try:
        page.wait_for_selector('[role="menuitemradio"], [role="menuitem"]',
                               timeout=REVIEWS["menu_timeout_ms"])
    except Exception:
        return                              # menu never opened
    page.evaluate(PICK_LOWEST_JS)


def _wait_until_sorted(page) -> bool:
    """Poll until the top of the list is low-rated, i.e. the re-sort landed.

    Opening the menu unmounts the list and it comes back on Google's schedule,
    not ours — a fixed wait was right often enough to look fine and wrong on
    about one lead in seven, losing that lead's complaints."""
    waited = 0
    while waited < REVIEWS["sort_verify_timeout_ms"]:
        stars = top_stars(page, 3)
        if stars and max(stars) <= REVIEWS["sort_verify_max_stars"]:
            return True
        page.wait_for_timeout(REVIEWS["sort_poll_ms"])
        waited += REVIEWS["sort_poll_ms"]
    return False


def sort_lowest(page) -> bool:
    """Re-sort the panel by lowest rating. True only once the list is *proven*
    re-sorted.

    Maps defaults to "Most relevant", which is overwhelmingly five-star praise
    — pleasant, and useless for spotting a business whose customers can't get
    hold of them. The complaints are the buying signal, so we go and get them.

    Trusting the click is not enough: it silently failed on 2 of 20 leads, and
    an all-praise result is indistinguishable from a business with no
    complaints. So the outcome is what's checked, not the click."""
    for attempt in range(REVIEWS["sort_attempts"]):
        try:
            _click_sort_lowest(page)
            if _wait_until_sorted(page):
                return True
            log.debug("sort didn't take, attempt %d", attempt + 1)
        except Exception as err:
            log.debug("review sort failed (%s)", type(err).__name__)
    return False
