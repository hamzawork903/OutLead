"""
The gate. Runs G1 to G7 in order over stored leads and records what happened.

Order is load-bearing. Each stage kills leads before the next one spends
anything on them, so the only leads the LLM ever reads are the ones that
survived every free check:

  G1 identity     duplicates, chains, closed          free
  G2 structural   thresholds, then Door-2 signals     free
  G3 contactable  real reachable address              free
  G4 judge        reads the reviews                   ~$0.0002
  G5 exclusion    quality ratio                       free
  G6/G7 score, tier, quotes                           free

Two rules hold everywhere:

  Nothing is deleted. A dropped lead keeps its row and its reason, so a
  threshold change re-runs against yesterday's rejects instead of needing a
  re-scrape — and you can tell "my gate is too tight" from "this city is done".

  The LLM can never drop a lead Door 2 already accepted, except by an explicit
  high-confidence veto. A failed call leaves the floor standing.
"""

import json
from datetime import datetime

from core.logbook import get_logger
from qualifier import (gate_contact, gate_identity, gate_structural, judge,
                       profiles, score as scoring)
from storage import gate_store

log = get_logger(__name__)


def _door_for(lead: dict, profile: dict, action: str) -> tuple:
    """(door, drop_reason). Door 1 is evidence, Door 2 is structure."""
    if action == "veto":
        return None, "complaints are about their own trade, not ours"
    signals = gate_structural.need_signals(lead, profile)
    required = gate_structural.signals_required(profile)
    if action == "upgrade":
        return 1, None
    if len(signals) >= required:
        return 2, None
    return None, ("no evidence and only %d of %d structural signal(s) needed"
                  % (len(signals), required))


def _cached_verdict(lead: dict) -> dict | None:
    """A verdict already stored for this lead, if it's usable.

    Re-reading the same reviews should give the same answer, and paying twice
    for it is the smaller problem — the real one is that it might NOT, leaving
    a lead that qualifies on Monday and doesn't on Tuesday."""
    try:
        stored = json.loads(lead.get("judge_verdict") or "null")
    except json.JSONDecodeError:
        return None
    if isinstance(stored, dict) and stored.get("status") == "ok":
        return stored
    return None


def _gate_one(lead: dict, profile: dict, suppressed: set, dupes: dict,
              rejudge: bool = False) -> dict:
    """Everything the gate decides about one lead, as a dict of columns."""
    now = datetime.now().isoformat(timespec="seconds")
    result = {"place_key": lead["place_key"], "gated_at": now,
              "gate_status": "dropped", "door": None, "tier": None,
              "priority_score": 0, "problem_type": None, "problem_summary": None,
              "gap": None, "offer": None, "evidence_count": 0,
              "quality_ratio": None, "praise_point": None, "quotes": None,
              "judge_verdict": None, "drop_reason": None}

    reason = (dupes.get(lead["place_key"])
              or gate_identity.check(lead, profile)
              or gate_structural.check(lead, profile)
              or gate_contact.check(lead, profile, suppressed))
    if reason:
        result["drop_reason"] = reason
        return result

    verdict = None if rejudge else _cached_verdict(lead)
    if verdict is None:
        verdict = judge.judge(lead, profile)
    action = judge.decide(verdict)
    result["judge_verdict"] = json.dumps(verdict)

    complaints, _ = judge.split_reviews(lead, profile)
    gate = profile.get("gate") or {}
    ratio = scoring.quality_ratio(complaints, profile.get("quality_complaint_terms"),
                                  gate.get("quality_ratio_min_sample", 4))
    result["quality_ratio"] = ratio

    # G5: a business whose complaints are mostly about its own work has a
    # problem we cannot fix, and would be a poor client if it bought anyway.
    if ratio is not None and ratio > gate.get("quality_complaint_ratio_max", 0.5):
        result["drop_reason"] = f"quality complaints dominate ({ratio:.0%})"
        return result

    door, drop = _door_for(lead, profile, action)
    if door is None:
        result["drop_reason"] = drop
        return result

    if verdict.get("status") == "ok":
        result["problem_type"] = verdict["problem_type"]
        result["problem_summary"] = verdict.get("problem_summary")
        result["praise_point"] = verdict.get("praise_point")
        result["evidence_count"] = verdict.get("matching_complaints", 0)

    # What we pitch them, decided by the gap rather than guessed at later.
    gap, offer = gate_structural.pick_offer(
        gate_structural.need_signals(lead, profile), profile,
        has_evidence=(door == 1))
    result["gap"] = gap
    result["offer"] = (offer or {}).get("sell")

    result["door"] = door
    result["priority_score"] = scoring.priority_score(verdict, complaints)
    result["tier"] = scoring.tier_for(door, result["priority_score"])
    result["gate_status"] = "lead"
    if door == 1:
        result["quotes"] = json.dumps(scoring.pick_quotes(complaints, verdict))
    return result


def run(conn, vertical: str, leads: list | None = None,
        rejudge: bool = False) -> dict:
    """Gate every stored lead of a vertical. Returns a summary of the funnel."""
    profile = profiles.load(vertical)
    if profile is None:
        return {}
    if leads is None:
        leads = gate_store.leads_for_vertical(conn, profile["slug"])
    if not leads:
        log.warning("gate: no stored leads for %r — scrape it first", vertical)
        return {}

    suppressed = gate_store.suppressed_emails(conn)
    dupes = gate_identity.duplicates(leads)
    log.info("gate: %s — %d lead(s) in, %d duplicate(s)",
             vertical, len(leads), len(dupes))

    summary = {"in": len(leads), "leads": 0, "door1": 0, "door2": 0,
               "dropped": 0, "reasons": {}}
    for lead in leads:
        result = _gate_one(lead, profile, suppressed, dupes, rejudge)
        gate_store.save_gate_result(conn, result)
        if result["gate_status"] == "lead":
            summary["leads"] += 1
            summary["door%d" % result["door"]] += 1
        else:
            summary["dropped"] += 1
            key = (result["drop_reason"] or "unknown").split(" (")[0]
            summary["reasons"][key] = summary["reasons"].get(key, 0) + 1

    log.info("gate: %d lead(s) — %d with evidence, %d structural; %d dropped",
             summary["leads"], summary["door1"], summary["door2"],
             summary["dropped"])
    for reason, count in sorted(summary["reasons"].items(),
                                key=lambda kv: -kv[1]):
        log.info("   dropped %3d x %s", count, reason)
    return summary
