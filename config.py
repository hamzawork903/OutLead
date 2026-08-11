"""
Control panel for the scraper. EVERY tunable lives here — timeouts, delays,
selectors, paths. If Google changes something or a run feels too fast/slow,
this is the only file to touch. No other module hardcodes these values.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------- paths ----
BASE_DIR = Path(__file__).parent

# Loads a local .env file (SMTP_EMAIL / SMTP_APP_PASSWORD) into the real
# environment if one exists at the project root — path given explicitly so
# this works regardless of the directory a script is launched from. .env is
# gitignored — never commit real credentials. A missing .env is a silent
# no-op (fine for every script except outreach.py --live, which needs these
# two variables set).
load_dotenv(BASE_DIR / ".env")
LOGS_DIR = BASE_DIR / "logs"
# Dedicated Chrome profile so a one-time manual login persists across runs
# (defeats Google's logged-out "sign in to see more" feed truncation).
# Never commit this folder — it holds live session cookies. Use a BURNER
# Google account here, never a personal or business one.
PROFILE_DIR = BASE_DIR / "profile"
DB_PATH = BASE_DIR / "leads.db"          # SQLite store (dedupe + accumulation)
EXPORTS_DIR = BASE_DIR / "exports"       # CSV spreadsheets, one per run

# -------------------------------------------------------------- browser ----
BROWSER = {
    # Headed by default: less detectable than headless, and you can watch it work.
    "headless": False,
    "viewport": {"width": 1366, "height": 850},
    "locale": "en-US",
    # A real, current Chrome-on-Windows user agent (no "HeadlessChrome" marker).
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# ------------------------------------------------- timeouts (milliseconds) ----
# Per-action budgets: a slow step fails fast and retries instead of hanging the run.
TIMEOUTS_MS = {
    "page_load": 30_000,       # full page navigation
    "element_wait": 10_000,    # waiting for a specific element to appear
    "click": 10_000,           # a single click action
    "search_results": 20_000,  # from pressing Enter to results feed visible
    "warmup": 60_000,          # first Maps load of the session (cold start)
}

# ----------------------------------------------------- delays (seconds) ----
# All randomized ranges — the humanizer draws from these, never fixed values.
DELAYS_S = {
    "between_keystrokes": (0.05, 0.18),   # typing speed, per character
    "before_action": (0.4, 1.2),          # small pause before any click/press
    "after_search": (2.0, 3.5),           # let results settle after searching
    "reading_pause": (1.5, 3.5),          # general "human is looking" pause
    "between_scrolls": (1.8, 3.4),        # feed scroll pacing (research: <1.5s
                                          # fixed intervals get soft-banned fast)
    "between_listings": (1.4, 3.0),       # pause between opening each listing
}

# ------------------------------------------------------------ collector ----
COLLECTOR = {
    "max_scrolls": 60,     # hard ceiling; ~120-result cap needs ~20 scrolls
    "stall_limit": 4,      # scrolls with no new results before we nudge
    "nudge_limit": 2,      # nudges before the long-cooldown last resort
    "progress_every": 3,   # print a progress line every N scrolls
    # Last resort when nudges fail: Google sometimes throttles feed paging
    # briefly. One long cooldown usually revives it; only then accept partial.
    "cooldown_s": (15.0, 25.0),
}

# ------------------------------------------------------------ selectors ----
# Layered: try stable attributes (role/aria/id) first, class names last.
# Verify with the --smoke run before long sessions — Google shuffles class names.
SELECTORS = {
    # Comma = fallback chain, most stable first. Google's new Maps UI (seen
    # 2026-07) dropped #searchboxinput; input[name="q"] survives both DOMs.
    "search_input": 'input[name="q"], #searchboxinput',
    "results_feed": 'div[role="feed"]',
    "result_card": 'a[href*="/maps/place/"]',
    # Exact text confirmed in the new UI (2026-07); we match a loose substring
    # in case Google tweaks punctuation.
    "end_of_list_text": "reached the end of the list",
    # When a query matches exactly one business, Maps skips the feed and opens
    # the place page directly. h1 inside the main panel identifies that case.
    "place_title": 'h1.DUwDvf, div[role="main"] h1',
    # Consent page (region-dependent; usually EU). Ordered by preference.
    "consent_buttons": [
        'button[aria-label="Accept all"]',
        'button[aria-label="Reject all"]',
        'form[action*="consent"] button',
    ],
}

# ------------------------------------------------------- review capture ----
# Reviews are the best raw material for outreach copy: what customers SAY
# about a business beats what the business says about itself. They're also
# the most expensive field we collect — they sit behind a tab click and a
# lazy-loading panel — so this only runs when the 'reviews_text' field group
# is switched on, and never blocks a lead from being saved if it fails.
REVIEWS = {
    "max_per_lead": 12,         # cap; more reviews = more scrolling = more risk
    # The mix is the point. All praise and there's no weakness to pitch; all
    # complaints and the email reads as an accusation. We open with something
    # true and good, then name the gap.
    "negative_target": 6,       # reviews at or below negative_max_stars
    "positive_target": 5,       # reviews at or above positive_min_stars
    "negative_max_stars": 3,    # Google reviews are whole stars: 1, 2, 3
    "positive_min_stars": 4,
    "scroll_rounds": 3,         # the lazy panel loads ~8-10 per round
    # A safety bound on hostile input, NOT formatting. Quotes are stored whole
    # so they can be quoted; the token budget lives at the prompt (LLM below).
    "max_chars_each": 2000,
    "panel_timeout_ms": 8000,   # give up on the panel rather than hang the run
    "tab_attempts": 4,          # the tab strip hydrates after the h1 does
    "tab_wait_ms": 600,         # pause between those attempts
    "scroll_wait_ms": 900,      # let each lazy-load land before scrolling again
    "expand_more": True,        # click "More" for untruncated review text
    "expand_passes": 3,         # expanding re-renders; later cards need another go
    "expand_wait_ms": 700,      # time for the expanded text to render
    # Maps sorts by "Most relevant", which is nearly all praise. The complaints
    # are the buying signal, so fetch a few of the worst on purpose.
    "include_lowest_rated": True,
    "lowest_rated_max": 3,      # kept first, so the char budget can't drop them
    "sort_wait_ms": 1200,       # list re-sort after picking an option
    "menu_timeout_ms": 2500,    # wait for the sort menu to render
    "sort_attempts": 2,         # the first click silently misses sometimes
    "sort_verify_max_stars": 3,  # top of a lowest-first list must be <= this
    "sort_verify_timeout_ms": 5000,  # how long to wait for the re-sort to land
    "sort_poll_ms": 400,        # how often to check while waiting
}

# ---------------------------------------------------------- reliability ----
# Retry only TRANSIENT failures (slow loads); backoff grows 2s -> 4s -> 8s
# with jitter so we never hammer a struggling page. Blocks are never retried.
RETRY = {
    "attempts": 3,          # total tries for a transient failure
    "base_delay_s": 2.0,    # first backoff wait
    "max_delay_s": 20.0,    # ceiling on backoff
}

# Rate discipline — protects the IP/account from throttling (the thing that
# bit us in testing). Tunable; raise the cap once we trust a proxy/rotation.
RATE = {
    "between_runs_s": 60,       # minimum gap between runs (waits out remainder)
    "daily_listing_cap": 500,  # max listings extracted per calendar day
}

# ---------------------------------------------------- email enrichment ----
# Visiting business websites (NOT Google) to find contact emails. Different
# domains, so we can safely run many in parallel — that's the speed win.
ENRICH = {
    "workers": 8,               # sites fetched in parallel (different servers)
    "page_timeout_s": 12.0,     # per-request network timeout
    "site_budget_s": 25.0,      # total time allowed per site (all pages)
    "max_pages": 4,             # homepage + up to 3 contact-ish pages
    "max_bytes": 2_000_000,     # ignore/trim pages larger than this (~2 MB)
    # Link text/URL hints that suggest a page likely to hold an email.
    "contact_hints": ["contact", "about", "team", "connect", "reach", "staff"],
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

MAPS_URL = "https://www.google.com/maps"

# ---------------------------------------------------- lead qualifier ----
# Phase 2, stage 1: score + segment leads BEFORE any outreach. Pure rules over
# fields we already scraped — no LLM, no network calls, runs instantly.
QUALIFY = {
    # Point values added to quality_score (0-100, clamped) per signal present.
    "points": {
        "email_valid": 30,      # a verified, deliverable email
        "email_risky": 15,      # an email exists but domain looks shaky
        "has_phone": 10,        # Maps phone OR a phone found on the website
        "has_website": 10,
        "has_social": 5,        # at least one social profile found
        "rating_4": 10,         # rating >= rating_good
        "rating_45": 5,         # extra, rating >= rating_great
        "reviews_20": 10,       # reviews >= reviews_established
        "reviews_100": 5,       # extra, reviews >= reviews_popular
        "open_now": 5,          # open_state == "Open"
        # From captured reviews (opt-in 'reviews_text' group; 0 when absent):
        "review_pain": 15,      # a customer complained they couldn't get through
                                # — the prospect's own market proving the pain
        "owner_engaged": 5,     # the owner replies to reviews = reachable, cares
    },
    "rating_good": 4.0,
    "rating_great": 4.5,
    "reviews_established": 20,
    "reviews_popular": 100,
    "reviews_high_volume": 50,   # threshold used for service_fit tiering
    "min_qualified_score": 30,   # below this -> qualify_status = "low_priority"
}

# ---------------------------------------------------------- outreach ----
# Phase 2, stage 2: free email sequences with automatic stop-on-reply.
#
# Credentials are NEVER stored here (this file is committed to git). Set
# these as environment variables before running outreach.py:
#   SMTP_EMAIL          the sending address, e.g. yourname@gmail.com
#   SMTP_APP_PASSWORD   a Gmail "App Password" (NOT your normal password) —
#                       create one at https://myaccount.google.com/apppasswords
#                       (requires 2-Step Verification to be turned on first)
SMTP = {
    "host": "smtp.gmail.com",
    "port": 587,
    "imap_host": "imap.gmail.com",   # for reading replies
    "imap_port": 993,
}

# Who your emails come from. Shown in every footer — an honest sender
# identity and a working opt-out are required for basic CAN-SPAM compliance.
# Set these in .env (see .env.example); the defaults below are placeholders
# and the sender refuses to run live until you replace them.
OUTREACH_IDENTITY = {
    "from_name": os.environ.get("OUTREACH_FROM_NAME", "Your Name"),
    "title": os.environ.get("OUTREACH_TITLE", "Founder"),
    "business_name": os.environ.get("OUTREACH_BUSINESS_NAME", "Your Company"),
    # href target; the email displays the business name, not the raw URL
    "website": os.environ.get("OUTREACH_WEBSITE", "example.com"),
    "calendly_url": os.environ.get("OUTREACH_BOOKING_URL",
                                   "https://calendly.com/your-handle/intro-call"),
    "reply_note": os.environ.get(
        "OUTREACH_REPLY_NOTE",
        "Not a fit? Just reply UNSUBSCRIBE and I'll show myself out for "
        "good — no hard feelings."),
}

# True while OUTREACH_IDENTITY is still the shipped placeholder — the live
# send path checks this so a fresh clone can never email strangers signed
# "Your Name" with someone else's booking link.
IDENTITY_IS_PLACEHOLDER = (
    OUTREACH_IDENTITY["from_name"] == "Your Name"
    or OUTREACH_IDENTITY["business_name"] == "Your Company"
)

# WHAT YOU SELL. This is the only description of your offer in the system —
# the LLM writes every email from it (enrichment/llm_writer.py), so make it
# concrete. Two sentences beats a paragraph. Set in .env.
OUTREACH_OFFER = {
    "what_you_sell": os.environ.get(
        "OUTREACH_WHAT_YOU_SELL",
        "a service that helps local businesses win more customers."),
    # The pain you lead with — the reason a stranger should care in line one.
    "angle": os.environ.get(
        "OUTREACH_ANGLE",
        "they are losing customers they already paid to attract, and a small "
        "fix would recover them."),
}

# ------------------------------------------------ mailbox verification ----
# Reoon Email Verifier (free tier: recurring monthly credits + API access).
# POWER mode asks the mail server whether the INDIVIDUAL inbox exists — the
# check our MX-only validation can't do — so non-existent addresses never
# get enqueued and never bounce. Needs REOON_API_KEY in .env; without it (or
# when credits run out / API is down) verification quietly falls back to the
# existing MX-based verdicts.
MAILBOX_VERIFY = {
    "enabled": True,
    "mode": "power",           # 'power' = real inbox check ('quick' can't)
    "timeout_s": 30.0,         # power mode can take a while on slow servers
    "catch_all_ok": False,     # catch-all domains can't be proven real ->
                               # 'risky' (kept out of outreach) unless True
}

# ---------------------------------------------------- LLM email writer ----
# The LLM writes each lead's ENTIRE 3-step sequence from that lead's real
# data (enrichment/llm_writer.py); validated output is stored on the lead and
# is the only thing the send path will send. Needs OPENAI_API_KEY in .env
# (never here). Without it, leads simply have no emails yet and sends skip
# loudly instead of falling back to canned copy.
LLM = {
    "enabled": True,
    "model": "gpt-4o-mini",        # ~$0.001 per lead for a full 3-step sequence
    "timeout_s": 45.0,             # full-sequence generation, not a one-liner
    "max_site_chars": 3500,        # how much website text the model sees
    "min_site_chars": 200,         # thinner than this -> model leans on trade+city
    # Reviews are stored in full; this is how much of them the model pays for.
    "review_count": 6,             # complaints come first, so they survive this
    "review_chars_total": 1800,
    # HARD spending cap for the test phase: once total estimated spend
    # (tracked per-call from the API's own token counts, persisted in
    # logs/llm_spend.json) reaches this, the LLM stops with a loud error.
    # Raise it here once the test proves the difference.
    "budget_usd": 5.00,
    "price_per_mtok": {"input": 0.15, "output": 0.60},   # gpt-4o-mini pricing
}

# The ONLY address the dashboard's "send test email" button can ever reach.
# Read once from the environment and never influenced by request data, so no
# amount of clicking (or a crafted request) can point a test at a real lead.
# Put YOUR OWN inbox here via .env — it exists so you can see exactly what a
# lead would receive. Defaults to the SMTP account itself.
TEST_SEND_ADDRESS = (os.environ.get("TEST_SEND_ADDRESS")
                     or os.environ.get("SMTP_EMAIL", ""))

SEQUENCE = {
    # Day offsets from the first send; first entry must be 0.
    "step_delay_days": [0, 3, 7],
    # Raised from 20 at the user's explicit request. WARNING: 100/day cold
    # email from a fresh Gmail with one day of sending history is far above
    # the recommended ramp (start 5-20/day, grow over 4-6 weeks). Watch for
    # bounces/spam placement and drop this back down if replies go quiet.
    "daily_send_cap": 100,
    "between_sends_s": (8.0, 20.0),   # human-like pacing between sends
    "reply_check_window_days": 30,    # how far back IMAP looks for replies
    "dry_run": False,                 # real sending armed — see the --live /
                                      # "send for real" checkbox as the second key
}
