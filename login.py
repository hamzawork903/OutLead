"""
One-time sign-in for the scraper's dedicated Chrome profile.

Run this ONCE:

    python login.py

A Chrome window opens on accounts.google.com. Sign in to your BURNER Google
account BY HAND — take your time. The script watches for a successful login,
saves the session into the profile folder, and closes itself. No password is
ever typed by the script or stored in the code; every future scrape simply
reuses this saved session.

You only need to run this again if the session eventually expires (weeks or
months), or if a run starts hitting the logged-out "sign in to see more" wall.
"""

import sys

from core.browser import is_signed_in, maps_session
from core.logbook import get_logger, setup_logging

LOGIN_URL = "https://accounts.google.com/"
POLL_SECONDS = 3
TIMEOUT_SECONDS = 420  # ~7 minutes of patience for a manual login

log = get_logger(__name__)


def main() -> int:
    log_path = setup_logging("login")
    log.info("\n  Opening Chrome. In the window that appears, sign in to your")
    log.info("  BURNER Google account (never a personal/business one).")
    log.info("  Take your time — this window closes itself once it sees you're")
    log.info("  signed in.\n")

    with maps_session(warm=False) as page:
        if is_signed_in(page.context):
            log.info("  This profile is already signed in — nothing to do.")
            return 0

        page.goto(LOGIN_URL)
        log.debug("waiting for manual login (poll %ds, timeout %ds)",
                  POLL_SECONDS, TIMEOUT_SECONDS)

        waited = 0
        while waited < TIMEOUT_SECONDS:
            page.wait_for_timeout(POLL_SECONDS * 1000)
            waited += POLL_SECONDS
            if is_signed_in(page.context):
                log.info("  Signed in — saving the session...")
                page.wait_for_timeout(2000)  # let all auth cookies settle
                log.info("  Done. Every future run reuses this login.")
                return 0

        log.warning("  Didn't see a completed sign-in within %d minutes. "
                    "Re-run  python login.py  to try again.",
                    TIMEOUT_SECONDS // 60)
        log.info("\n  Full log: %s", log_path)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Login cancelled. Nothing saved.")
        sys.exit(130)
