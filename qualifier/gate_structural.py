"""
G2 — structural. The baseline a business must clear, and Door 2.

Two different questions, deliberately kept apart:

  baseline   is this business worth pitching at all? rating, volume, contact
             details, and whether they're a real outfit (accreditations)

  need       does their SETUP suggest they're losing calls, even if no
             customer has written about it? that's Door 2

Door 2 exists because Door 1 — a customer complaining in a review — throws away
too much. Plenty of businesses have the problem badly and nobody happened to
write it down. But a lead with no evidence can only ever be Tier C, and the
email may not imply we know something we don't.

Which signals apply is per-vertical, from the profile. A 5pm closing time is
damning for an emergency plumber and meaningless for a bakery, so
`emergency_scenario: null` switches that test off entirely.
"""

import json

from core.logbook import get_logger

log = get_logger(__name__)

_BOOKING_WORDS = ("book online", "book now", "online booking", "book an appointment",
                  "schedule online", "request a quote", "booking form")
_ALWAYS_OPEN = ("open 24 hours", "24 hours", "24/7", "24-7", "open 24")
_URGENCY_WORDS = ("24/7", "24-7", "24 hour", "24hr", "emergency", "out of hours",
                  "same day", "anytime", "round the clock")


def _text_of(lead: dict) -> str:
    return f"{lead.get('name') or ''} {lead.get('website_text') or ''}".lower()


def accredited(lead: dict, profile: dict) -> str | None:
    """The trade body found on their site, or None.

    Better proof they're a real, established business than a review count: a
    Gas Safe number is a fact, 25 reviews is a guess."""
    text = _text_of(lead)
    for body in profile.get("accreditations") or []:
        if body.lower() in text:
            return body
    return None


def claims_always_open(lead: dict) -> bool:
    """Do they advertise 24/7 or emergency cover, in the name or on the site?"""
    return any(word in _text_of(lead) for word in _URGENCY_WORDS)


def hours_are_24_7(lead: dict) -> bool:
    return any(word in (lead.get("hours") or "").lower() for word in _ALWAYS_OPEN)


def applicable_signals(profile: dict) -> int:
    """How many Door-2 signals can fire at all for this vertical.

    Not every signal applies everywhere: the hours test is meaningless without
    an emergency scenario, and a missing booking link says nothing in a trade
    where nobody books online. Requiring a fixed 2 out of 4 when only 2 are
    possible closed Door 2 completely for plumbers — 30 leads, zero structural
    passes — so the requirement has to be relative to this number."""
    count = 1                               # never-replies-to-reviews, always on
    if profile.get("emergency_scenario"):
        count += 1
    if profile.get("books_online") in ("often", "sometimes"):
        count += 1
    return count


def signals_required(profile: dict) -> int:
    """How many signals this vertical needs to open Door 2: half of what can
    apply, never fewer than one, never more than the profile asks for."""
    configured = (profile.get("gate") or {}).get("structural_signals_required", 2)
    applicable = applicable_signals(profile)
    return max(1, min(configured, (applicable + 1) // 2))


def need_signals(lead: dict, profile: dict) -> list:
    """Which Door-2 gaps this lead has, as keys.

    Keys rather than prose because each one now routes to something we sell —
    see `offers` in the profile. A gap isn't just evidence they need help, it
    decides what we offer them, which means the email never has to guess at an
    angle.

    Deliberately no "no email published" gap here: G3 drops those leads before
    this runs, so it could never fire — it only made the threshold look
    reachable when it wasn't. It stays in `offers` because the harvester will
    eventually reach them through a contact page."""
    gaps = []

    # The strongest one, and free: they advertise cover they don't staff.
    if profile.get("emergency_scenario") and claims_always_open(lead) \
            and lead.get("hours") and not hours_are_24_7(lead):
        gaps.append("hours_mismatch")

    # Only meaningful where the trade normally books online. For drainage
    # nobody does, so its absence says nothing.
    if profile.get("books_online") in ("often", "sometimes"):
        text = lead.get("website_text") or ""
        if text and not any(word in text.lower() for word in _BOOKING_WORDS):
            gaps.append("no_booking")

    reviews = _reviews_of(lead)
    if len(reviews) >= 5 and not any(r.get("owner_replied") for r in reviews):
        gaps.append("no_review_replies")

    return gaps


def pick_offer(gaps: list, profile: dict, has_evidence: bool = False) -> tuple:
    """(gap key, offer) — what to pitch this lead, or (None, None).

    Review evidence outranks every structural gap: a customer saying they
    couldn't get through beats anything inferred from a website. Otherwise the
    profile's priority order decides, so a lead with three gaps gets pitched
    the most valuable one rather than whichever happened to be checked first."""
    offers = profile.get("offers") or {}
    candidates = ["phone_evidence"] if has_evidence else []
    candidates += [g for g in gaps if g in offers]
    if not candidates:
        return None, None
    best = min(candidates, key=lambda g: offers.get(g, {}).get("priority", 99))
    return best, offers.get(best)


def _reviews_of(lead: dict) -> list:
    raw = lead.get("reviews_text")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def check(lead: dict, profile: dict) -> str | None:
    """Baseline drop reason, or None. Pure comparisons — the cheapest gate we
    have, so it runs before anything that costs a query or a token."""
    gate = profile.get("gate") or {}

    rating = lead.get("rating")
    if rating is None:
        return "no rating"
    if rating < gate.get("rating_min", 0):
        return f"rating {rating} below {gate['rating_min']}"
    ceiling = gate.get("rating_max")
    if ceiling and rating > ceiling:
        return f"rating {rating} above {ceiling}"

    reviews = lead.get("reviews") or 0
    if reviews < gate.get("review_count_min", 0):
        return f"only {reviews} reviews, needs {gate['review_count_min']}"
    cap = gate.get("review_count_max")
    if cap and reviews > cap:
        return f"{reviews} reviews above {cap} — likely has a call centre"

    if gate.get("requires_website") and not lead.get("website"):
        return "no website"
    if gate.get("requires_phone") and not lead.get("phone"):
        return "no phone"
    return None
