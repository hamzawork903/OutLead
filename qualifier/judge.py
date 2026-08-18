"""
G4/G5 — the judge. The only part of the gate that reads what customers wrote.

Keyword lists cannot do this job. "Left a message and they rang me right back"
and "left a message, never heard back" share the keyword, and a list written
for UK trades has no idea what a relevant complaint sounds like for a dental
practice. It took reading twenty Austin listings by hand to notice their
complaints were about billing rather than phones — that is the observation this
module exists to make automatically, on lead one.

It answers three questions per business:

  is anyone complaining about something WE fix?     -> upgrade to Tier A/B
  nothing conclusive?                               -> leave the Door-2 floor
  are the complaints about their actual craft?      -> veto, drop the lead

That last one matters as much as the first. A firm whose customers say the work
was botched has a business problem, not a phone problem: they won't buy, and if
they did they'd be a bad client.

Biased toward dropping. A wrong pass costs a real email to a real person and a
piece of your sender reputation; a wrong drop costs a lead, and there are
always more leads. Low confidence means drop.
"""

import json

from config import LLM
from core import llm
from core.dates import is_recent, parse_relative
from core.logbook import get_logger
from qualifier.judge_prompt import _brief, _system_prompt

log = get_logger(__name__)

PROBLEM_TYPES = ("phone", "booking", "billing", "quality", "none")
CONFIDENCES = ("high", "medium", "low")

# Verdicts that mean "they have a problem we sell into".
_OURS = ("phone", "booking")


def validate(raw: str, complaint_count: int) -> dict | None:
    """Parse and check the verdict. None if anything is off — an unparseable
    verdict must never be read as a pass."""
    try:
        data = json.loads((raw or "").strip())
    except (json.JSONDecodeError, AttributeError):
        log.warning("judge returned non-JSON — discarding")
        return None
    if not isinstance(data, dict):
        return None

    problem = str(data.get("problem_type", "")).lower().strip()
    confidence = str(data.get("confidence", "")).lower().strip()
    if problem not in PROBLEM_TYPES or confidence not in CONFIDENCES:
        log.warning("judge returned unknown problem_type/confidence (%r/%r)",
                    problem, confidence)
        return None

    matching = data.get("matching_complaints")
    matching = matching if isinstance(matching, int) and matching >= 0 else None

    index = data.get("best_quote_index")
    if not isinstance(index, int) or not 0 <= index < max(complaint_count, 1):
        index = None

    praise = data.get("praise_point")
    praise = str(praise).strip()[:200] if praise else None

    return {
        "problem_type": problem,
        "phone_evidence": bool(data.get("phone_evidence")),
        "healthy_business": bool(data.get("healthy_business")),
        "lost_customer": bool(data.get("lost_customer")),
        "out_of_hours": bool(data.get("out_of_hours")),
        "matching_complaints": matching,
        "best_quote_index": index,
        "praise_point": praise or None,
        "confidence": confidence,
        # The one field a human actually reads. "phone" as a verdict is
        # useless on its own — you can't judge a lead or write a pitch from a
        # single word, so this carries the actual finding.
        "problem_summary": str(data.get("problem_summary")
                               or data.get("reason") or "")[:800],
    }


def split_reviews(lead: dict, profile: dict) -> tuple:
    """(complaints, praise) — recent complaints the gate may use as evidence,
    and positives for the praise line. Stale evidence is dropped here: a
    two-year-old complaint proves nothing about today and quoting it looks
    sloppy."""
    raw = lead.get("reviews_text")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [], []
    if not isinstance(raw, list):
        return [], []

    max_months = (profile.get("gate") or {}).get("evidence_max_age_months", 12)
    complaints, praise = [], []
    for review in raw:
        stars = review.get("stars")
        if stars is None:
            continue
        if stars <= profile.get("negative_max_stars", 3):
            posted = review.get("date")
            when = (parse_relative(review.get("when")) if not posted
                    else _as_date(posted))
            if is_recent(when, max_months):
                complaints.append(review)
        else:
            praise.append(review)
    return complaints, praise[:3]


def _as_date(iso: str):
    from datetime import date
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def judge(lead: dict, profile: dict) -> dict:
    """Read one business's reviews and return a verdict. Costs about $0.0002.

    Always returns a dict carrying a `status`, because "this business has no
    complaints to read" and "the API call failed" mean completely different
    things and must never look alike — one is a finding, the other is us being
    broken. Collapsing both into None hid three failed calls in the first
    trial run, including the strongest lead in the set."""
    complaints, praise = split_reviews(lead, profile)
    if not complaints:
        return {"status": "no_complaints"}

    raw = llm.ask_json(_system_prompt(profile),
                       _brief(lead, complaints, praise),
                       max_tokens=LLM.get("judge_max_tokens", 300),
                       label=repr(lead.get("name")),
                       temperature=LLM.get("judge_temperature", 0.0))
    if raw is None:
        return {"status": "call_failed"}

    verdict = validate(raw, len(complaints))
    if verdict is None:
        return {"status": "unreadable_verdict"}
    verdict["status"] = "ok"
    verdict["complaints_seen"] = len(complaints)
    # Evidence means complaints about OUR problem, not every complaint we
    # showed it. Fall back to the total only if the model didn't answer.
    if verdict.get("matching_complaints") is None:
        verdict["matching_complaints"] = len(complaints)
    verdict["matching_complaints"] = min(verdict["matching_complaints"],
                                         len(complaints))
    return verdict


def decide(verdict: dict | None) -> str:
    """What the verdict means for the lead: 'upgrade', 'veto' or 'neutral'.

    Kept apart from the model call so the policy is testable without a network,
    and so the one place that can drop a lead reads as five lines rather than
    being buried in a prompt. Anything that isn't a clean verdict is neutral —
    a failed call must never drop a lead that Door 2 already let in."""
    if not verdict or verdict.get("status") != "ok":
        return "neutral"
    if verdict["confidence"] == "low":
        return "neutral"                    # unsure never drops AND never promotes

    # A veto throws away a lead Door 2 already accepted, so it takes the
    # strongest evidence we allow. Medium confidence is not enough to decide a
    # business is failing at its own trade.
    if verdict["confidence"] == "high" \
            and verdict["problem_type"] in ("quality", "billing") \
            and not verdict["healthy_business"]:
        return "veto"

    # phone_evidence is not required for a booking problem — a customer who was
    # double-booked never mentions the phone, and demanding it here meant every
    # correctly-identified booking lead silently failed to promote.
    if verdict["problem_type"] in _OURS and verdict["healthy_business"]:
        return "upgrade"
    return "neutral"
