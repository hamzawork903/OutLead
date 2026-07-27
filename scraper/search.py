"""
Runs a search on an already-warmed-up Maps page and confirms results actually
loaded before returning. Handles both outcomes Google can produce:

  - a results FEED (the normal case for "dentists in Austin, TX")
  - a single PLACE page (when the query matches exactly one business)

Returns a SearchResult so the caller knows which case it got.
"""

from dataclasses import dataclass

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from config import SELECTORS, TIMEOUTS_MS
from core import humanizer
from core.browser import save_failure_screenshot
from core.logbook import get_logger
from core.reliability import guard_block

log = get_logger(__name__)


@dataclass
class SearchResult:
    query: str
    kind: str            # "feed" | "single_place" | "no_results"
    visible_count: int   # result cards visible before any scrolling (feed only)


def run_search(page, query: str) -> SearchResult:
    """Type the query, submit, and wait for a definite outcome."""
    log.info('  Searching for "%s"...', query)
    box = page.locator(SELECTORS["search_input"]).first
    box.click()
    box.fill("")  # clear any previous query so re-searches start clean
    humanizer.type_like_human(page, SELECTORS["search_input"], query)
    humanizer.pause("before_action")
    page.keyboard.press("Enter")
    log.debug("query submitted, waiting for outcome")

    outcome = _wait_for_outcome(page)
    log.debug("search outcome: %s", outcome)
    guard_block(page)  # a search can trip Google's challenge; stop if so
    humanizer.pause("after_search")  # let lazy content settle before scraping

    if outcome == "feed":
        count = page.locator(SELECTORS["result_card"]).count()
        log.debug("results feed present, %d cards visible pre-scroll", count)
        return SearchResult(query=query, kind="feed", visible_count=count)
    if outcome == "single_place":
        log.debug("single-place page (query matched exactly one business)")
        return SearchResult(query=query, kind="single_place", visible_count=1)
    log.debug("no-results page")
    return SearchResult(query=query, kind="no_results", visible_count=0)


def _wait_for_outcome(page) -> str:
    """
    Poll for whichever appears first: the results feed, a single place page,
    or Google's "can't find" message. Polling beats a single wait_for here
    because we're racing three different outcomes.
    """
    deadline_ms = TIMEOUTS_MS["search_results"]
    step_ms = 250
    waited = 0
    while waited < deadline_ms:
        if page.locator(SELECTORS["results_feed"]).count() > 0:
            return "feed"
        if page.locator(SELECTORS["place_title"]).count() > 0:
            return "single_place"
        if page.get_by_text("Google Maps can't find").count() > 0:
            return "no_results"
        page.wait_for_timeout(step_ms)
        waited += step_ms

    # No feed/place/no-results — a challenge is a common hidden cause, so check.
    guard_block(page)
    shot = save_failure_screenshot(page, "search-timeout")
    log.error("search produced no recognizable outcome in %ds (screenshot: %s)",
              deadline_ms // 1000, shot)
    raise PlaywrightTimeout(
        f"Search gave no results feed, place page, or 'not found' message "
        f"within {deadline_ms // 1000}s."
    )
