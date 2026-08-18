"""Offline checks for the gate. No network, no LLM, temp DB only.

Covers the decisions that can lose a lead or let a bad one through:
identity, thresholds, contactability, the judge's verdict policy, and scoring.

Run: python tests/test_gate.py
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logbook import setup_logging          # noqa: E402
setup_logging("test-gate")

passed = 0


def check(name, cond, detail=""):
    global passed
    assert cond, f"FAILED: {name} {detail}"
    passed += 1
    print(f"  ok: {name}")


from qualifier import (gate_contact, gate_identity, gate_structural,  # noqa: E402
                       judge, profiles, score as scoring)

PROFILE = profiles.load("Emergency Plumbing")
check("profile loads", PROFILE is not None and PROFILE["tier"] == 1)

# ------------------------------------------------------- G1 identity ----
check("chain matched inside a longer name",
      gate_identity.is_chain("Dyno-Rod Manchester", PROFILE["chains"]) == "dyno-rod")
check("independent firm is not a chain",
      gate_identity.is_chain("Baker & Sons Plumbing", PROFILE["chains"]) is None)
check("closed business dropped",
      "closed" in gate_identity.check({"open_state": "Permanently closed"}, PROFILE))
check("phone normalised for comparison",
      gate_identity.phone_key("+44 161 496 0100") ==
      gate_identity.phone_key("0161 496 0100"))
check("facebook page is not an identity",
      gate_identity.domain_key("https://facebook.com/someplumber") == "")
check("real domain is an identity",
      gate_identity.domain_key("https://www.acme-plumbing.co.uk/x") == "acme-plumbing.co.uk")

dupes = gate_identity.duplicates([
    {"place_key": "a", "name": "Acme", "phone": "0161 496 0100", "website": "", "address": "1 St"},
    {"place_key": "b", "name": "Acme North", "phone": "+44 161 496 0100", "website": "", "address": "2 St"},
    {"place_key": "c", "name": "Other", "phone": "0161 496 0999", "website": "", "address": "3 St"},
])
check("second listing on the same phone is a duplicate", "b" in dupes)
check("first listing is kept", "a" not in dupes)
check("unrelated business untouched", "c" not in dupes)

# ----------------------------------------------------- G2 structural ----
good = {"rating": 4.4, "reviews": 62, "website": "http://x.co.uk", "phone": "1"}
check("healthy lead passes the baseline", gate_structural.check(good, PROFILE) is None)
check("low rating dropped", "below" in gate_structural.check({**good, "rating": 3.2}, PROFILE))
check("too few reviews dropped", "needs" in gate_structural.check({**good, "reviews": 4}, PROFILE))
check("no website dropped", gate_structural.check({**good, "website": ""}, PROFILE) == "no website")
check("no rating at all dropped", gate_structural.check({**good, "rating": None}, PROFILE) == "no rating")

# Door-2 signals
claims = {**good, "name": "Acme 24 Hour Emergency Plumbing",
          "hours": "Mon 9 AM to 5 PM; Tue 9 AM to 5 PM", "email": "a@x.co.uk"}
signals = gate_structural.need_signals(claims, PROFILE)
check("advertises emergency cover but closes at 5 is a gap",
      "hours_mismatch" in signals, str(signals))
always = {**claims, "hours": "Open 24 hours"}
check("genuinely 24/7 is not a gap",
      "hours_mismatch" not in gate_structural.need_signals(always, PROFILE))
# "no email published" was removed as a signal: G3 drops those leads first, so
# it could never fire and only made the threshold look reachable.
check("no-email is NOT a gap here (G3 already dropped those)",
      "no_email" not in gate_structural.need_signals({**claims, "email": None}, PROFILE))
check("plumbing has 2 applicable signals",
      gate_structural.applicable_signals(PROFILE) == 2)
check("...so it needs only 1 to open Door 2",
      gate_structural.signals_required(PROFILE) == 1)
med = profiles.load("Med Spa / Aesthetics Clinic")
check("a trade that books online has more applicable signals",
      gate_structural.applicable_signals(med) >= 2)
check("requirement never exceeds the configured cap",
      gate_structural.signals_required(med) <= 2)

no_replies = {**good, "email": "a@x.co.uk", "reviews_text": json.dumps(
    [{"text": "x" * 20, "stars": 1, "owner_replied": False}] * 6)}
check("never replying to reviews is a gap",
      "no_review_replies" in gate_structural.need_signals(no_replies, PROFILE))

# ---- national numbers and wrong trades ----
check("0333 number is not a local trade",
      gate_identity.is_national("+44 333 000 0000", PROFILE))
check("0800 number is not a local trade",
      gate_identity.is_national("0800 000 0000", PROFILE))
check("a real Manchester landline passes",
      not gate_identity.is_national("+44 161 496 0765", PROFILE))
check("a mobile passes", not gate_identity.is_national("07700 900123", PROFILE))
check("wrong trade for the vertical is dropped",
      "wrong trade" in (gate_identity.check(
          {"category": "Boiler supplier", "phone": "0161 496 0100"},
          profiles.load("Solar & Battery Installation")) or ""))
check("the right trade passes",
      gate_identity.check({"category": "Plumber", "phone": "0161 496 0100"},
                          PROFILE) is None)
check("a missing category is not a reason to drop",
      gate_identity.check({"phone": "0161 496 0100"}, PROFILE) is None)

# ---- every gap must route to something we actually sell ----
for gap in ("hours_mismatch", "no_booking", "no_review_replies", "no_email",
            "phone_evidence"):
    key, offer = gate_structural.pick_offer([gap], PROFILE, has_evidence=False)         if gap != "phone_evidence" else         gate_structural.pick_offer([], PROFILE, has_evidence=True)
    check(f"gap '{gap}' routes to an offer", offer and offer.get("sell"), str(offer))
check("review evidence outranks a structural gap",
      gate_structural.pick_offer(["no_booking"], PROFILE, has_evidence=True)[0]
      == "phone_evidence")
check("highest-priority gap wins when several fire",
      gate_structural.pick_offer(["no_booking", "no_review_replies"], PROFILE)[0]
      == "no_review_replies")
check("no gaps and no evidence means nothing to pitch",
      gate_structural.pick_offer([], PROFILE) == (None, None))

# A vertical with no emergency scenario must not use the hours test at all.
solar = profiles.load("Solar & Battery Installation")
check("hours test off where there's no emergency scenario",
      "hours_mismatch" not in
      gate_structural.need_signals({**claims, "email": "a@x.co.uk"}, solar))
check("requirement is never zero, even with one applicable signal",
      gate_structural.signals_required(solar) >= 1)

check("accreditation found in site text",
      gate_structural.accredited({"website_text": "We are Gas Safe registered"}, PROFILE)
      == "gas safe")
check("no accreditation returns None",
      gate_structural.accredited({"website_text": "we do plumbing"}, PROFILE) is None)

# ----------------------------------------------------- G3 contact ----
check("vendor address dropped",
      "vendor" in gate_contact.check({"email": "bugreport@moatable.com",
                                      "email_status": "valid"}, PROFILE))
check("real business address passes",
      gate_contact.check({"email": "info@acme.co.uk", "email_status": "valid"},
                         PROFILE) is None)
check("nhs.net is never auto-blocked",
      gate_contact.check({"email": "a@nhs.net", "email_status": "valid"},
                         PROFILE) is None)
check("suppressed address dropped",
      gate_contact.check({"email": "a@x.co.uk", "email_status": "valid"},
                         PROFILE, {"a@x.co.uk"}) == "on the suppression list")
check("invalid email dropped",
      gate_contact.check({"email": "a@x.co.uk", "email_status": "invalid"},
                         PROFILE) == "email invalid")
check("missing email dropped",
      gate_contact.check({}, PROFILE) == "no email found")

# ------------------------------------------- G4 verdict validation ----
ok_json = json.dumps({"problem_type": "phone", "phone_evidence": True,
                      "healthy_business": True, "lost_customer": True,
                      "out_of_hours": True, "best_quote_index": 1,
                      "praise_point": "same-day callout", "confidence": "high",
                      "reason": "three reviews mention no answer"})
v = judge.validate(ok_json, 3)
check("valid verdict parses", v and v["problem_type"] == "phone")
check("lost_customer carried through", v["lost_customer"] is True)
check("quote index kept when in range", v["best_quote_index"] == 1)
check("quote index dropped when out of range",
      judge.validate(ok_json, 1)["best_quote_index"] is None)
check("echoed enum is rejected",
      judge.validate(json.dumps({"problem_type": "phone|booking|billing",
                                 "confidence": "high"}), 3) is None)
check("unknown confidence is rejected",
      judge.validate(json.dumps({"problem_type": "phone",
                                 "confidence": "pretty sure"}), 3) is None)
check("non-JSON is rejected", judge.validate("not json at all", 3) is None)
check("empty is rejected", judge.validate("", 3) is None)

# ------------------------------------------------ G4 decision policy ----
def verdict(**kw):
    base = {"status": "ok", "problem_type": "phone", "phone_evidence": True,
            "healthy_business": True, "confidence": "high"}
    return {**base, **kw}


check("phone problem at a healthy firm upgrades",
      judge.decide(verdict()) == "upgrade")
check("booking problem upgrades without phone evidence",
      judge.decide(verdict(problem_type="booking", phone_evidence=False)) == "upgrade")
check("craft complaints at an unhealthy firm veto",
      judge.decide(verdict(problem_type="quality", healthy_business=False)) == "veto")
check("veto needs HIGH confidence",
      judge.decide(verdict(problem_type="quality", healthy_business=False,
                           confidence="medium")) == "neutral")
check("low confidence never promotes", judge.decide(verdict(confidence="low")) == "neutral")
check("no problem found is neutral", judge.decide(verdict(problem_type="none")) == "neutral")
check("a failed call never drops a lead",
      judge.decide({"status": "call_failed"}) == "neutral")
check("an unreadable verdict never drops a lead",
      judge.decide({"status": "unreadable_verdict"}) == "neutral")
check("no verdict at all is neutral", judge.decide(None) == "neutral")

# ------------------------------------------------------- G6 scoring ----
today = date.today()
fresh = [{"text": "nobody answered", "stars": 1,
          "date": (today - timedelta(days=10)).isoformat()}] * 3
stale = [{"text": "nobody answered", "stars": 1,
          "date": (today - timedelta(days=900)).isoformat()}] * 3

full = scoring.priority_score(verdict(lost_customer=True, out_of_hours=True,
                                      matching_complaints=3), fresh)
check("everything firing scores high", full >= 90, str(full))
# Evidence is complaints about OUR problem, not every complaint shown. iHeat
# had 7 complaints and 1 real booking failure, and scored Tier A off the total.
partial = scoring.priority_score(
    verdict(lost_customer=False, out_of_hours=False, complaints_seen=7,
            matching_complaints=1), fresh)
allofthem = scoring.priority_score(
    verdict(lost_customer=False, out_of_hours=False, complaints_seen=7,
            matching_complaints=7), fresh)
check("one matching complaint scores far below seven", partial < allofthem,
      f"{partial} vs {allofthem}")
check("unmatched complaints add nothing", partial == 10 + 15 + 10, str(partial))
stale_score = scoring.priority_score(
    verdict(lost_customer=True, out_of_hours=True, matching_complaints=3), stale)
check("stale evidence scores lower than fresh", stale_score < full,
      f"{stale_score} vs {full}")
check("...by exactly the freshness points",
      full - stale_score == min(15, scoring.POINTS["fresh_evidence"]) or full == 100,
      f"{full} - {stale_score}")
check("no verdict scores zero", scoring.priority_score(None, fresh) == 0)
check("failed call scores zero",
      scoring.priority_score({"status": "call_failed"}, fresh) == 0)
check("score is capped at 100", full <= 100)

check("evidence + high score is tier A", scoring.tier_for(1, 85) == "A")
check("evidence + low score is tier B", scoring.tier_for(1, 30) == "B")
check("structural only is always tier C", scoring.tier_for(2, 95) == "C")
check("no door means no tier", scoring.tier_for(None, 80) is None)

quotes = scoring.pick_quotes(
    [{"text": "first", "stars": 1, "date": "2026-01-01"},
     {"text": "judge pick", "stars": 1, "date": "2025-06-01"}],
    verdict(best_quote_index=1))
check("the judge's pick leads", quotes[0]["text"] == "judge pick")
check("two quotes returned", len(quotes) == 2)
check("no complaints means no quotes", scoring.pick_quotes([], verdict()) == [])

terms = PROFILE["quality_complaint_terms"]
craft = [{"text": "botched the job and left a mess"}] * 3 + [{"text": "no answer"}]
check("quality ratio computed", scoring.quality_ratio(craft, terms, 4) == 0.75)
check("too small a sample returns None",
      scoring.quality_ratio(craft[:2], terms, 4) is None)

print(f"\nALL {passed} GATE CHECKS PASSED")
