"""
The lead qualifier — Phase 2, stage 1. Runs BEFORE any outreach.

Pure rules over fields we already scraped in Phase 1 — no LLM, no network
calls, no cost, instant. Every lead gets three things:

  quality_score   0-100. How worth contacting is this lead? (reachability +
                  signs the business has budget/traffic: rating, reviews,
                  verified email, phone, website, socials, currently open)
  service_fit     which of OUR services best fits their digital gap:
                    "web_development" — no website, or a website with no
                                         real online presence (no email/social)
                    "automation"      — has a digital presence but modest
                                         volume; needs to systemize follow-up,
                                         booking, review requests
                    "ai"              — established, high-volume business;
                                         ready for an AI agent to handle scale
  qualify_status  "qualified" | "low_priority" | "skip"
                    skip = closed, or literally no way to reach them
                    low_priority = reachable but low quality_score
                    qualified = everything else

Operates on a plain dict (works with sqlite3.Row via dict(row) or a CSV row),
so it has no dependency on any one Lead shape.
"""

from dataclasses import dataclass

from config import QUALIFY
from core.logbook import get_logger

log = get_logger(__name__)

SOCIAL_FIELDS = ("facebook", "instagram", "linkedin", "youtube", "tiktok", "twitter")
CLOSED_STATES = {"Permanently closed", "Temporarily closed"}


@dataclass
class Qualification:
    quality_score: int
    service_fit: str | None
    fit_reason: str | None
    qualify_status: str


def _get(row, key):
    """Tolerate missing columns (older DB rows before a migration) as None."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def has_social(row) -> bool:
    return any(_get(row, f) for f in SOCIAL_FIELDS)


def has_phone(row) -> bool:
    return bool(_get(row, "phone") or _get(row, "website_phones"))


def is_reachable(row) -> bool:
    """Any channel at all to contact this business."""
    return bool(_get(row, "email") or has_phone(row) or has_social(row))


def is_closed(row) -> bool:
    return _get(row, "open_state") in CLOSED_STATES


def score_lead(row) -> int:
    """0-100 quality score from signals present on the row."""
    p = QUALIFY["points"]
    score = 0

    email_status = _get(row, "email_status")
    if email_status == "valid":
        score += p["email_valid"]
    elif email_status == "risky":
        score += p["email_risky"]

    if has_phone(row):
        score += p["has_phone"]
    if _get(row, "website"):
        score += p["has_website"]
    if has_social(row):
        score += p["has_social"]

    rating = _get(row, "rating")
    if rating is not None:
        if rating >= QUALIFY["rating_good"]:
            score += p["rating_4"]
        if rating >= QUALIFY["rating_great"]:
            score += p["rating_45"]

    reviews = _get(row, "reviews")
    if reviews is not None:
        if reviews >= QUALIFY["reviews_established"]:
            score += p["reviews_20"]
        if reviews >= QUALIFY["reviews_popular"]:
            score += p["reviews_100"]

    if _get(row, "open_state") == "Open":
        score += p["open_now"]

    return max(0, min(100, score))


def fit_service(row) -> tuple[str | None, str | None]:
    """(service_fit, reason). None if the lead is closed (no pitch applies)."""
    if is_closed(row):
        return None, None

    website = _get(row, "website")
    if not website:
        return "web_development", "no website found — needs a digital presence"

    online_presence = bool(_get(row, "email")) or has_social(row)
    if not online_presence:
        return ("web_development",
                "has a website but no findable email or social profiles — "
                "weak or outdated online presence")

    reviews = _get(row, "reviews") or 0
    if reviews >= QUALIFY["reviews_high_volume"]:
        return ("ai",
                f"established business ({reviews} reviews) — ready for an AI "
                f"agent to handle inquiry volume")
    return ("automation",
            "has an online presence but modest volume — good fit for "
            "automating follow-ups, bookings, and review requests")


def status_for(row, score: int) -> str:
    if is_closed(row):
        return "skip"
    if not is_reachable(row):
        return "skip"
    if score < QUALIFY["min_qualified_score"]:
        return "low_priority"
    return "qualified"


def qualify(row) -> Qualification:
    """Run the full qualifier on one lead row."""
    score = score_lead(row)
    fit, reason = fit_service(row)
    status = status_for(row, score)
    return Qualification(quality_score=score, service_fit=fit,
                         fit_reason=reason, qualify_status=status)
