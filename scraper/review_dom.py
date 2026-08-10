"""
Everything that touches Google's review DOM. The policy — how many to keep,
what counts as junk — lives in reviews.py; this file only knows how to drive
the panel and read what's in it.

The reviews DOM is generated React with churn-prone class names, unlike the
`data-item-id` anchors the rest of the extractor leans on, so every selector
here is chosen to survive a redesign:

  container   div[data-review-id]              a real data attribute
  stars       [role="img"][aria-label*="star"] accessibility text
  text        longest leaf text node before the owner's reply
  controls    matched by aria-label / jsaction, never by class

Every function fails soft — the caller keeps its lead either way.
"""

from config import REVIEWS
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

# Read every loaded review card. Returns raw strings; all cleaning happens in
# Python where it's testable.
_REVIEWS_JS = r"""(maxCount) => {
    const OWNER_RE = /response from the owner/i;
    const cards = [...document.querySelectorAll('div[data-review-id]')]
        // review cards carry an aria-label with the reviewer name; the
        // container for the *whole list* also matches, so keep only leaves
        .filter(el => !el.querySelector('div[data-review-id]'));

    const out = [];
    for (const card of cards.slice(0, maxCount)) {
        const starEl = card.querySelector('[role="img"][aria-label*="star"], [aria-label*="star"]');
        const stars = starEl ? (starEl.getAttribute('aria-label') || '') : '';

        const leaves = [...card.querySelectorAll('span, div')]
            .filter(el => !el.querySelector('span, div'));

        // The owner's reply must not be mistaken for the review. Excluding it
        // by container fails: textContent bubbles, so every ancestor up to the
        // card "contains" the phrase. Anchor on the leaf holding the label
        // instead — the reply is always what follows it in document order.
        const label = leaves.find(el => OWNER_RE.test(el.textContent || ''));

        // The review body is the longest text block before that anchor. Class
        // names change; length doesn't.
        let best = '';
        for (const el of leaves) {
            if (label && (label.compareDocumentPosition(el) &
                          Node.DOCUMENT_POSITION_FOLLOWING)) continue;
            if (OWNER_RE.test(el.textContent || '')) continue;
            const t = (el.textContent || '').trim();
            if (t.length > best.length) best = t;
        }

        // Relative date: short text ending in "ago", or an absolute month.
        let when = null;
        for (const el of card.querySelectorAll('span')) {
            const t = (el.textContent || '').trim();
            if (/\b(ago|week|month|year|day)s?\b/i.test(t) && t.length < 30) { when = t; break; }
        }

        out.push({stars: stars, text: best, when: when, owner_replied: !!label});
    }
    return out;
}"""

# Google collapses long reviews behind a "More" control. Match it by label and
# by jsaction as well as by text, so a wording change doesn't silently start
# storing truncated previews. Scoped to review cards on purpose: Maps has other
# buttons reading "More", and clicking one of those tears down the panel.
_EXPAND_JS = r"""() => {
    let clicked = 0;
    for (const b of document.querySelectorAll(
            'div[data-review-id] button, div[data-review-id] [role="button"]')) {
        const text = (b.textContent || '').trim();
        const meta = (b.getAttribute('aria-label') || '') + ' ' +
                     (b.getAttribute('jsaction') || '');
        if (/^(more|see more|read more)$/i.test(text) ||
            /see more|expandReview/i.test(meta)) {
            try { b.click(); clicked++; } catch (e) {}
        }
    }
    return clicked;
}"""

# Nudge the list down one screen so the lazy loader fetches more. Stops at the
# container holding the cards: walking further up reaches Maps' results feed,
# and scrolling *that* mid-scrape unmounts the listing panel.
_SCROLL_JS = r"""() => {
    const card = document.querySelector('div[data-review-id]');
    if (!card) return;
    let el = card.parentElement;
    for (let i = 0; i < 6 && el; i++) {
        // the listing header and the results feed both live above the reviews
        // list; if we can see either, we've climbed too far
        if (el.scrollHeight > el.clientHeight + 40 &&
            !el.querySelector('h1') && !el.querySelector('[role="feed"]')) {
            el.scrollTop += el.clientHeight;
            return;
        }
        el = el.parentElement;
    }
}"""

_OPEN_SORT_JS = r"""() => {
    const b = [...document.querySelectorAll('button')].find(
        el => /sort reviews/i.test(el.getAttribute('aria-label') || ''));
    if (!b) return false;
    b.click();
    return true;
}"""

_PICK_LOWEST_JS = r"""() => {
    const item = [...document.querySelectorAll(
        '[role="menuitemradio"], [role="menuitem"], [role="option"]')].find(
        el => /lowest/i.test(el.textContent || ''));
    if (!item) return false;
    item.click();
    return true;
}"""


def find_tab(page):
    """The Reviews tab, or None if this listing genuinely hasn't got one.
    Retries briefly: the tab strip hydrates a moment after the h1, so a single
    look loses reviews on whichever listings happen to render slowly."""
    for attempt in range(REVIEWS["tab_attempts"]):
        for sel in _TAB_SELECTORS:
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
        return page.evaluate(_REVIEWS_JS, limit) or []
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
            found = page.evaluate(_EXPAND_JS)
        except Exception as err:
            log.debug("review expand pass failed (%s)", type(err).__name__)
            break
        clicked += found
        if not found:
            break
        page.wait_for_timeout(REVIEWS["expand_wait_ms"])
    return clicked


def scroll_once(page) -> None:
    """Scroll the reviews list one screen to pull in the next lazy batch."""
    try:
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(REVIEWS["scroll_wait_ms"])
    except Exception as err:
        log.debug("review scroll failed (%s)", type(err).__name__)


def sort_lowest(page) -> bool:
    """Re-sort the panel by lowest rating. True if the list was re-sorted.

    Maps defaults to "Most relevant", which is overwhelmingly five-star praise
    — pleasant, and useless for spotting a business whose customers can't get
    hold of them. The complaints are the buying signal, so we go and get them."""
    try:
        if not page.evaluate(_OPEN_SORT_JS):
            return False
        page.wait_for_timeout(REVIEWS["sort_wait_ms"])
        if not page.evaluate(_PICK_LOWEST_JS):
            page.keyboard.press("Escape")       # leave the menu as we found it
            return False
        page.wait_for_timeout(REVIEWS["sort_wait_ms"])
        return True
    except Exception as err:
        log.debug("review sort failed (%s)", type(err).__name__)
        return False
