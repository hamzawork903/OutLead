"""
Browser lifecycle: launch Chromium with stealth patches, then warm up Google
Maps until it is fully interactive before handing control to any scraper
module. Cold-start slowness is absorbed HERE, once — downstream modules can
assume a ready, responsive Maps page.
"""

import time
from contextlib import contextmanager
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from config import BROWSER, LOGS_DIR, MAPS_URL, PROFILE_DIR, SELECTORS, TIMEOUTS_MS
from core.logbook import get_logger

log = get_logger(__name__)

# Presence of any of these cookies on a google.com domain means the profile
# has a live signed-in Google session.
GOOGLE_AUTH_COOKIES = {"SAPISID", "__Secure-1PSID", "SID"}


def is_signed_in(context) -> bool:
    """True if the browser profile currently holds a Google login session."""
    names = {c["name"] for c in context.cookies()
             if "google.com" in c.get("domain", "")}
    signed = bool(names & GOOGLE_AUTH_COOKIES)
    log.debug("sign-in check: %s", "signed in" if signed else "logged out")
    return signed

# Masks the fingerprints Google checks first: navigator.webdriver, empty
# plugin list, missing languages. Applied before any page script runs.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def save_failure_screenshot(page, label: str) -> str | None:
    """Snapshot the page into logs/ so we can see what Google was showing."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        path = LOGS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.png"
        page.screenshot(path=str(path))
        log.debug("saved failure screenshot: %s", path)
        return str(path)
    except Exception:
        log.debug("could not save screenshot for %r", label, exc_info=True)
        return None  # a screenshot failure must never mask the real error


def _dismiss_consent(page) -> bool:
    """Clear Google's consent page if it appears (region-dependent)."""
    for selector in SELECTORS["consent_buttons"]:
        button = page.locator(selector).first
        try:
            if button.is_visible(timeout=1_500):
                log.debug("consent button matched: %s", selector)
                button.click(timeout=TIMEOUTS_MS["click"])
                page.wait_for_load_state("domcontentloaded")
                return True
        except PlaywrightTimeout:
            continue
    return False


@contextmanager
def maps_session(warm: bool = True, locale: str | None = None):
    """
    Yields a Playwright page backed by our persistent Chrome profile, so a
    one-time manual login (see login.py) carries into every run.

    Args:
        warm: run the Maps warm-up before yielding. Login flow passes False
              because it drives accounts.google.com, not Maps.

    Usage:
        with maps_session() as page:
            ...scrape...
    """
    with sync_playwright() as pw:
        PROFILE_DIR.mkdir(exist_ok=True)
        log.debug("launching Chromium, persistent profile=%s (headless=%s)",
                  PROFILE_DIR, BROWSER["headless"])
        # Persistent context = one object, no separate browser handle; cookies
        # and storage live in PROFILE_DIR between runs.
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=BROWSER["headless"],
            args=["--disable-blink-features=AutomationControlled"],
            viewport=BROWSER["viewport"],
            locale=locale or BROWSER["locale"],
            user_agent=BROWSER["user_agent"],
        )
        context.add_init_script(STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if warm:
                _warm_up(page)
            yield page
        finally:
            log.debug("closing browser")
            context.close()


def _warm_up(page, attempts: int = 2) -> None:
    """Load Maps and wait until the search box is genuinely usable."""
    started = time.time()
    log.info("  Opening Google Maps (first load can take a moment)...")
    for attempt in range(1, attempts + 1):
        try:
            log.debug("warm-up attempt %d/%d: goto %s", attempt, attempts, MAPS_URL)
            page.goto(MAPS_URL, timeout=TIMEOUTS_MS["warmup"],
                      wait_until="domcontentloaded")
            log.debug("navigation resolved, url=%s", page.url)
            if _dismiss_consent(page):
                log.info("  Cleared the cookie/consent screen.")
            # "Loaded" isn't enough — wait until the search box accepts input.
            box = page.locator(SELECTORS["search_input"]).first
            box.wait_for(state="visible", timeout=TIMEOUTS_MS["warmup"])
            box.click(timeout=TIMEOUTS_MS["click"])
            log.info("  Maps is ready (%.1fs).", time.time() - started)
            return
        except PlaywrightTimeout:
            if attempt < attempts:
                log.warning("slow Maps load — retrying (attempt %d of %d)",
                            attempt + 1, attempts)
                continue
            shot = save_failure_screenshot(page, "warmup-failed")
            hint = f" Screenshot: {shot}" if shot else ""
            log.exception(
                "Google Maps didn't become ready after %d attempts of %ds. "
                "Check your internet connection and try again.%s",
                attempts, TIMEOUTS_MS["warmup"] // 1000, hint,
            )
            raise
