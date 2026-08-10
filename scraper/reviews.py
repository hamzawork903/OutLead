"""
Review capture — the one field that needs its own interaction.

Everything else in a listing is in the panel Maps already rendered, so the
extractor reads it in a single evaluate(). Reviews are behind a tab click and
a lazy-loading scroll panel, which makes this both the slowest field and the
most fragile: the reviews DOM is generated React with churn-prone class names,
unlike the `data-item-id` anchors the rest of the extractor leans on.

So this module is written to FAIL SOFT. Every step is guarded and any problem
returns whatever was gathered so far (usually nothing) rather than raising —
a lead with no reviews is still a perfectly good lead, and losing the whole
scrape because Google reshuffled a class name would be absurd.

Selector strategy, most stable first:
  container   div[data-review-id]        — a real data attribute, survives redesigns
  stars       [role="img"][aria-label*="star"]  — accessibility text, rarely changes
  text        longest text node in the container that isn't the name/date
  owner reply detected by phrase, not class
"""

import re

from config import REVIEWS, TIMEOUTS_MS
from core.logbook import get_logger

log = get_logger(__name__)

# "Reviews" tab / button. Maps has shipped several shapes of this; try them in
# order of how stable they've proven, and fall back to matching by text.
_TAB_SELECTORS = (
    'button[role="tab"][aria-label*="Reviews"]',
    'button[aria-label*="Reviews for"]',
    'button[jsaction*="pane.reviewChart.moreReviews"]',
    'button[role="tab"]:has-text("Reviews")',
)

_STARS_RE = re.compile(r"([0-9](?:\.[0-9])?)\s*star", re.IGNORECASE)
_OWNER_REPLY_RE = re.compile(r"response from the owner", re.IGNORECASE)

# Runs inside the page: read every loaded review card. Returns raw strings;
# all cleaning happens in Python where it's testable.
_REVIEWS_JS = r"""(maxCount) => {
    const cards = [...document.querySelectorAll('div[data-review-id]')]
        // review cards carry an aria-label with the reviewer name; the
        // container for the *whole list* also matches, so keep only leaves
        .filter(el => !el.querySelector('div[data-review-id]'));

    const out = [];
    for (const card of cards.slice(0, maxCount)) {
        const starEl = card.querySelector('[role="img"][aria-label*="star"], [aria-label*="star"]');
        const stars = starEl ? (starEl.getAttribute('aria-label') || '') : '';

        // The review body is the longest text block in the card that isn't
        // the owner's reply. Class names change; length doesn't.
        const ownerBlock = [...card.querySelectorAll('div')]
            .find(d => /response from the owner/i.test(d.textContent || ''));
        let best = '';
        for (const el of card.querySelectorAll('span, div')) {
            if (ownerBlock && ownerBlock.contains(el)) continue;
            if (el.querySelector('span, div')) continue;      // leaf nodes only
            const t = (el.textContent || '').trim();
            if (t.length > best.length) best = t;
        }

        // Relative date: short text ending in "ago", or an absolute month.
        let when = null;
        for (const el of card.querySelectorAll('span')) {
            const t = (el.textContent || '').trim();
            if (/\b(ago|week|month|year|day)s?\b/i.test(t) && t.length < 30) { when = t; break; }
        }

        out.push({
            stars: stars,
            text: best,
            when: when,
            owner_replied: !!ownerBlock,
        });
    }
    return out;
}"""


def _find_tab(page):
    for sel in _TAB_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return el
        except Exception:                       # selector unsupported / detached
            continue
    return None


def _scroll_panel(page, rounds: int) -> None:
    """Scroll the reviews list so the lazy loader fetches more. Targets the
    scrollable ancestor of the review cards rather than a class name."""
    for _ in range(rounds):
        try:
            page.evaluate("""() => {
                const card = document.querySelector('div[data-review-id]');
                if (!card) return;
                let el = card.parentElement;
                while (el && el.scrollHeight <= el.clientHeight) el = el.parentElement;
                if (el) el.scrollTop = el.scrollHeight;
            }""")
            page.wait_for_timeout(900)          # let the lazy load land
        except Exception:
            return


def _expand_more(page) -> None:
    """Click the 'More' buttons so we capture full review text, not a
    truncated preview. Best-effort: any failure just means shorter text."""
    try:
        page.evaluate("""() => {
            for (const b of document.querySelectorAll('button')) {
                const label = (b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '');
                if (/^\\s*(More|See more)\\s*$/i.test(b.textContent || '') ||
                    /see more/i.test(label)) {
                    try { b.click(); } catch (e) {}
                }
            }
        }""")
        page.wait_for_timeout(400)
    except Exception:
        pass


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


def collect_reviews(page, business_name: str = "") -> list:
    """Open the reviews tab on an already-loaded listing and return up to
    REVIEWS['max_per_lead'] cleaned reviews. Returns [] on any failure —
    never raises, never costs the caller its lead."""
    tab = _find_tab(page)
    if tab is None:
        log.debug("%s: no reviews tab found (business may have none)", business_name)
        return []

    try:
        tab.click(timeout=TIMEOUTS_MS["click"])
        page.wait_for_selector("div[data-review-id]",
                               timeout=REVIEWS["panel_timeout_ms"])
    except Exception as err:
        log.debug("%s: reviews panel didn't open (%s)", business_name,
                  type(err).__name__)
        return []

    _scroll_panel(page, REVIEWS["scroll_rounds"])
    if REVIEWS["expand_more"]:
        _expand_more(page)

    try:
        raw = page.evaluate(_REVIEWS_JS, REVIEWS["max_per_lead"])
    except Exception as err:
        log.warning("%s: review extraction failed (%s) — continuing without "
                    "reviews", business_name, type(err).__name__)
        return []

    reviews = _clean(raw or [])
    log.debug("%s: captured %d review(s)", business_name, len(reviews))
    return reviews
