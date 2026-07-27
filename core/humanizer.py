"""
The timing engine. Every wait, pause, and keystroke delay in the scraper goes
through here — no other module calls sleep() or uses a fixed delay. That keeps
our pacing consistent, tunable from config.py, and human-looking (randomized,
never metronomic — fixed intervals are a classic bot fingerprint).
"""

import random
import time

from config import DELAYS_S
from core.logbook import get_logger

log = get_logger(__name__)


def pause(kind: str) -> None:
    """Sleep for a randomized duration drawn from the config range `kind`."""
    lo, hi = DELAYS_S[kind]
    wait = random.uniform(lo, hi)
    log.debug("pause[%s] %.2fs", kind, wait)
    time.sleep(wait)


def pause_range(lo: float, hi: float) -> float:
    """Sleep for a randomized duration in [lo, hi] seconds; returns the wait."""
    wait = random.uniform(lo, hi)
    log.debug("pause[range %.1f-%.1f] %.2fs", lo, hi, wait)
    time.sleep(wait)
    return wait


def type_like_human(page, selector: str, text: str) -> None:
    """Click a field and type into it with per-character randomized delays."""
    pause("before_action")
    field = page.locator(selector).first  # .first: selectors are fallback chains
    field.click()
    for char in text:
        lo, hi = DELAYS_S["between_keystrokes"]
        field.type(char, delay=random.uniform(lo, hi) * 1000)
