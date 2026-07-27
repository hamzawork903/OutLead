"""
Scrolls the results feed and harvests listing URLs. Collection only — no
per-listing extraction happens here, so one broken listing can never break
the scroll. Selectors were verified live against the new Maps UI (2026-07).

End conditions, in order of trust:
  1. Google's own "reached the end of the list" marker (the happy path)
  2. Stall handling: no new results after several scrolls -> nudge the feed
     (scroll up, then back down); after too many nudges, accept a partial
     list and say so honestly.
"""

import re

from config import COLLECTOR, SELECTORS
from core import humanizer
from core.browser import save_failure_screenshot
from core.ids import place_key_from_url
from core.logbook import get_logger
from core.models import Listing

log = get_logger(__name__)


def collect_listings(page, quiet: bool = False,
                     target: int | None = None) -> tuple[list[Listing], bool]:
    """
    Scroll until the end of the results feed, harvesting as we go.

    Args:
        target: if set, stop as soon as we have this many listings (a quick,
                gentle run instead of the full feed).

    Returns (listings, complete) — `complete` is False when we gave up on a
    stalled feed and the list may be missing entries.
    """
    seen: dict[str, Listing] = {}
    stalls = 0
    nudges = 0
    cooled_down = False

    _harvest(page, seen)
    log.debug("initial harvest: %d listings before scrolling", len(seen))
    for scroll_num in range(1, COLLECTOR["max_scrolls"] + 1):
        if target and len(seen) >= target:
            log.debug("target of %d reached at scroll %d — stopping early",
                      target, scroll_num)
            return list(seen.values())[:target], True
        if _at_end_of_list(page):
            _harvest(page, seen)
            log.debug("end-of-list marker found at scroll %d, total=%d",
                      scroll_num, len(seen))
            return list(seen.values()), True

        before = len(seen)
        _scroll_feed_to_bottom(page)
        humanizer.pause("between_scrolls")
        _harvest(page, seen)
        log.debug("scroll %d: %d -> %d listings", scroll_num, before, len(seen))

        if not quiet and scroll_num % COLLECTOR["progress_every"] == 0:
            log.info("  ...%d businesses found so far", len(seen))

        if len(seen) == before:
            # Quiet scroll: often Google is just loading the next batch
            # slowly. Give it one extra beat before calling it a stall.
            humanizer.pause("reading_pause")
            _harvest(page, seen)

        if len(seen) == before:
            stalls += 1
            if stalls >= COLLECTOR["stall_limit"]:
                if nudges < COLLECTOR["nudge_limit"]:
                    nudges += 1
                    stalls = 0
                    log.debug("stall limit reached -> nudge %d/%d at %d listings",
                              nudges, COLLECTOR["nudge_limit"], len(seen))
                    if not quiet:
                        log.info("  Feed went quiet — giving it a nudge...")
                    _nudge_feed(page)
                elif not cooled_down:
                    # Nudges didn't help — likely brief server-side paging
                    # throttle. One long cooldown, then a final attempt.
                    cooled_down = True
                    stalls = 0
                    shot = save_failure_screenshot(page, "feed-stall")
                    lo, hi = COLLECTOR["cooldown_s"]
                    log.debug("nudges exhausted at %d listings; cooldown "
                              "%.0f-%.0fs (screenshot: %s)", len(seen), lo, hi, shot)
                    if not quiet:
                        log.info("  Feed still quiet at %d — cooling down up to "
                                 "%.0fs before one last try...", len(seen), hi)
                    humanizer.pause_range(lo, hi)
                    _nudge_feed(page)
                else:
                    log.warning("Feed stalled for good at %d results — "
                                "keeping what we have (list is partial).",
                                len(seen))
                    return list(seen.values()), False
        else:
            stalls = 0

    log.warning("Hit the scroll ceiling (%d) at %d results — keeping what we "
                "have (list is partial).", COLLECTOR["max_scrolls"], len(seen))
    return list(seen.values()), False


def _harvest(page, seen: dict[str, Listing]) -> None:
    """Pull every place link currently in the feed into `seen` (dedup by URL)."""
    cards = page.eval_on_selector_all(
        f'{SELECTORS["results_feed"]} {SELECTORS["result_card"]}',
        "els => els.map(el => ({ url: el.href, name: el.getAttribute('aria-label') || '' }))",
    )
    for card in cards:
        # place_key (Google's ChIJ id) uniquely identifies the business and is
        # stable across renders — the right key for dedup and crash-resume.
        key = place_key_from_url(card["url"])
        # Google appends "Visited link" to the aria-label of already-visited
        # results — strip it so the name stays clean.
        name = re.sub(r"\s*·?\s*Visited link\s*$", "", card["name"]).strip()
        if key not in seen and name:
            seen[key] = Listing(name=name, url=card["url"], place_key=key)


def _scroll_feed_to_bottom(page) -> None:
    page.eval_on_selector(
        SELECTORS["results_feed"],
        "el => { el.scrollTop = el.scrollHeight; }",
    )


def _nudge_feed(page) -> None:
    """Scroll up a screen, pause, then back down — unsticks a quiet feed."""
    page.eval_on_selector(
        SELECTORS["results_feed"],
        "el => { el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight); }",
    )
    humanizer.pause("reading_pause")
    _scroll_feed_to_bottom(page)
    humanizer.pause("between_scrolls")


def _at_end_of_list(page) -> bool:
    feed = page.locator(SELECTORS["results_feed"]).first
    marker = feed.get_by_text(
        re.compile(SELECTORS["end_of_list_text"], re.IGNORECASE)
    )
    return marker.count() > 0
