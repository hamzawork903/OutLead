"""
G6/G7 — priority score, tier, and the two quotes that go in the email.

The score answers "who do I email first", which matters more than it sounds:
your first 25 emails should be the 25 best businesses, not the 25 that happened
to be scraped first.

Weighting follows the plan. `lost_customer` is worth more than everything else
combined for a reason — a customer writing "I gave up and went elsewhere" is
documented revenue walking out of the door, in their own words, and it's the
most persuasive thing you can put in front of an owner.

Tier is not a score band, it's which door the lead came through:

  A / B   Door 1 — a customer described the problem. Quote-backed.
  C       Door 2 — only their setup suggests it. No quotes, no implied
          knowledge; the email may not pretend to know something it doesn't.
"""

from datetime import date

from core.dates import is_recent
from core.logbook import get_logger

log = get_logger(__name__)

POINTS = {
    "lost_customer": 40,     # documented revenue leaving
    "per_complaint": 10,     # weight of evidence...
    "complaints_cap": 30,    # ...up to here
    "out_of_hours": 15,      # names the hour their cover dies
    "fresh_evidence": 15,    # the problem is happening now, not in 2023
    "phone_problem": 10,
}
FRESH_MONTHS = 3
TIER_A_SCORE = 70


def _newest(complaints: list) -> date | None:
    """Date of the most recent complaint we have a date for."""
    dates = []
    for review in complaints:
        try:
            dates.append(date.fromisoformat(review["date"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(dates) if dates else None


def priority_score(verdict: dict, complaints: list) -> int:
    """0-100. Zero when there's no verdict — a Door-2 lead has no evidence to
    score, and pretending otherwise would sort it above leads that do."""
    if not verdict or verdict.get("status") != "ok":
        return 0
    score = 0
    if verdict.get("lost_customer"):
        score += POINTS["lost_customer"]
    score += min(verdict.get("matching_complaints", 0) * POINTS["per_complaint"],
                 POINTS["complaints_cap"])
    if verdict.get("out_of_hours"):
        score += POINTS["out_of_hours"]
    if is_recent(_newest(complaints), FRESH_MONTHS):
        score += POINTS["fresh_evidence"]
    if verdict.get("problem_type") == "phone":
        score += POINTS["phone_problem"]
    return min(score, 100)


def tier_for(door: int | None, score: int) -> str | None:
    """A, B, C — or None for a lead that got through no door at all."""
    if door == 1:
        return "A" if score >= TIER_A_SCORE else "B"
    if door == 2:
        return "C"
    return None


def pick_quotes(complaints: list, verdict: dict, limit: int = 2) -> list:
    """The quotes that go into the email, verbatim, newest first.

    The judge's pick leads, because it read them and we didn't. Verbatim is the
    point: a customer's clumsy sentence is more convincing than a tidy one, and
    these are stored whole precisely so they can be used unedited."""
    if not complaints:
        return []
    chosen = []
    best = (verdict or {}).get("best_quote_index")
    if isinstance(best, int) and 0 <= best < len(complaints):
        chosen.append(complaints[best])

    rest = sorted((c for i, c in enumerate(complaints) if i != best),
                  key=lambda c: c.get("date") or "", reverse=True)
    chosen.extend(rest)
    return [{"text": c.get("text"), "date": c.get("date") or c.get("when"),
             "stars": c.get("stars")} for c in chosen[:limit]]


def quality_ratio(complaints: list, terms: list, min_sample: int) -> float | None:
    """Share of complaints that are about the trade's own work rather than
    reaching them. None when there aren't enough to judge — one review saying
    "damaged" on a sample of one is not evidence a business is failing."""
    if len(complaints) < min_sample:
        return None
    lowered = [(c.get("text") or "").lower() for c in complaints]
    hits = sum(1 for text in lowered if any(t in text for t in terms or []))
    return round(hits / len(complaints), 2)
