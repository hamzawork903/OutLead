"""
The safety layer. Three ideas, applied everywhere we touch Google:

  1. Error taxonomy. Failures are not equal:
       - TRANSIENT (slow load, flaky navigation) -> retry with backoff.
       - BLOCK (CAPTCHA / "unusual traffic" / sorry page) -> STOP immediately.
         Pushing through a block is what gets accounts flagged; we never do it.
       - (Missing fields are handled in the extractor as normal facts, not here.)

  2. Retry with exponential backoff + jitter, so a struggling page gets space
     instead of a hammering.

  3. Block detection that fails loud and safe: screenshot, clear message, and
     a BlockDetected exception the caller turns into a clean, resumable stop.
"""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from config import RETRY
from core import humanizer
from core.browser import save_failure_screenshot
from core.logbook import get_logger

log = get_logger(__name__)

# Transient errors worth retrying (network/navigation hiccups, slow elements).
TRANSIENT = (PlaywrightTimeout, PlaywrightError)


class BlockDetected(Exception):
    """Google is challenging us. We stop the run (progress is saved) rather
    than push through — the operator + a cooldown handle it, not brute force."""

    def __init__(self, kind: str, screenshot: str | None = None):
        super().__init__(kind)
        self.kind = kind
        self.screenshot = screenshot


_BLOCK_URL_HINTS = ("/sorry/", "/sorry?", "google.com/sorry")
_BLOCK_TEXT_HINTS = (
    "unusual traffic",
    "not a robot",
    "our systems have detected",
    "verify it's you",
    "verify you're a human",
)


def detect_block(page) -> str | None:
    """Return a short block kind if Google is challenging us, else None."""
    url = (getattr(page, "url", "") or "").lower()
    if any(hint in url for hint in _BLOCK_URL_HINTS):
        return "sorry-page"

    try:
        if page.locator(
            'iframe[src*="recaptcha"], iframe[title*="recaptcha"], '
            'div#recaptcha, form#captcha-form'
        ).count() > 0:
            return "captcha"
    except Exception:
        pass

    try:
        body = (page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        body = ""
    if any(hint in body for hint in _BLOCK_TEXT_HINTS):
        return "unusual-traffic"
    return None


def guard_block(page) -> None:
    """Raise BlockDetected (with a screenshot) if the page is a challenge."""
    kind = detect_block(page)
    if kind:
        shot = save_failure_screenshot(page, f"blocked-{kind}")
        log.error("Google is challenging us (%s). Stopping to protect the "
                  "account — your progress is saved; resume with the same "
                  "command later.", kind)
        raise BlockDetected(kind, shot)


def retry(action, *, label: str, attempts: int | None = None):
    """
    Run `action` (a no-arg callable), retrying TRANSIENT failures with
    exponential backoff + jitter. A BlockDetected is never retried — it
    propagates immediately so the run can stop cleanly.
    """
    attempts = attempts or RETRY["attempts"]
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except BlockDetected:
            raise
        except TRANSIENT as err:
            last_error = err
            if attempt >= attempts:
                break
            delay = min(RETRY["max_delay_s"],
                        RETRY["base_delay_s"] * (2 ** (attempt - 1)))
            log.warning("%s failed (%s) — retry %d/%d in ~%.0fs",
                        label, type(err).__name__, attempt + 1, attempts, delay)
            humanizer.pause_range(delay * 0.8, delay * 1.2)
    log.debug("%s exhausted all %d attempts", label, attempts)
    raise last_error
