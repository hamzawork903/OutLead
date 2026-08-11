"""
The JavaScript we run inside Google's review panel, and the selectors that
find it. Kept apart from review_dom.py for the same reason email templates are
kept apart from the code that sends them: this is page code in another
language, and it churns on Google's schedule, not ours.

Every selector here is chosen to survive a redesign:

  container   div[data-review-id]              a real data attribute
  stars       [role="img"][aria-label*="star"] accessibility text
  text        longest leaf text node before the owner's reply
  controls    matched by aria-label / jsaction, never by class
"""

# "Reviews" tab / button. Maps has shipped several shapes of this; try them in
# order of how stable they've proven, and fall back to matching by text.
TAB_SELECTORS = (
    'button[role="tab"][aria-label*="Reviews"]',
    'button[aria-label*="Reviews for"]',
    'button[jsaction*="pane.reviewChart.moreReviews"]',
    'button[role="tab"]:has-text("Reviews")',
)

# Read every loaded review card. Returns raw strings; all cleaning happens in
# Python where it's testable.
REVIEWS_JS = r"""(maxCount) => {
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
EXPAND_JS = r"""() => {
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

# Fallback for layouts with no "More" button at all: Maps expands some cards
# when the truncated text itself is clicked. Only ever touches text that is
# visibly cut off, so it can't disturb a card we already read in full.
EXPAND_TRUNCATED_JS = r"""() => {
    let clicked = 0;
    for (const card of document.querySelectorAll('div[data-review-id]')) {
        const cut = [...card.querySelectorAll('span, div')]
            .filter(el => !el.querySelector('span, div'))
            .find(el => (el.textContent || '').trim().endsWith('…'));
        if (cut) { try { cut.click(); clicked++; } catch (e) {} }
    }
    return clicked;
}"""

# Nudge the list down one screen so the lazy loader fetches more. Stops at the
# container holding the cards: walking further up reaches Maps' results feed,
# and scrolling *that* mid-scrape unmounts the listing panel.
SCROLL_JS = r"""() => {
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

# Maps renders the sort control as a button on some layouts and a plain
# role="button" on others, so match on the label rather than the tag.
OPEN_SORT_JS = r"""() => {
    const els = [...document.querySelectorAll('button, [role="button"]')];
    const b = els.find(el => /sort reviews|sort by/i.test(
                  el.getAttribute('aria-label') || ''))
           || els.find(el => /^\s*sort\s*$/i.test(el.textContent || ''));
    if (!b) return false;
    b.click();
    return true;
}"""

# Anchored at the start of the label: the menu's own container element holds
# the text of every option, so a loose /lowest/ match can select the wrapper
# and click nothing useful.
PICK_LOWEST_JS = r"""() => {
    const item = [...document.querySelectorAll(
        '[role="menuitemradio"], [role="menuitem"], [role="option"]')].find(
        el => /^lowest/i.test((el.textContent || '').trim()));
    if (!item) return false;
    item.click();
    return true;
}"""

# Star labels of the first few cards — used to prove a sort actually applied.
TOP_STARS_JS = r"""(n) => {
    return [...document.querySelectorAll('div[data-review-id]')]
        .filter(el => !el.querySelector('div[data-review-id]'))
        .slice(0, n)
        .map(card => {
            const s = card.querySelector('[role="img"][aria-label*="star"], [aria-label*="star"]');
            return s ? (s.getAttribute('aria-label') || '') : '';
        });
}"""

# The sort control sits at the top of the panel. After the lazy-load scrolling
# it is off-screen, and a menu opened from off-screen never becomes visible —
# which is how three leads in twenty lost their complaints.
SCROLL_TOP_JS = r"""() => {
    const card = document.querySelector('div[data-review-id]');
    if (!card) return;
    let el = card.parentElement;
    for (let i = 0; i < 6 && el; i++) {
        if (el.scrollHeight > el.clientHeight + 40 &&
            !el.querySelector('h1') && !el.querySelector('[role="feed"]')) {
            el.scrollTop = 0;
            return;
        }
        el = el.parentElement;
    }
}"""
