"""Offline checks for review capture: cleaning, storage round-trip,
qualifier signals, and the LLM prompt. No network, temp DB only."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.logbook import setup_logging
setup_logging("test-reviews")

passed = 0
def check(name, cond, detail=""):
    global passed
    assert cond, f"FAILED: {name} {detail}"
    passed += 1
    print(f"  ok: {name}")

# ---------- cleaning (the boundary where hostile UGC is tamed) ----------
from scraper.reviews import _clean, _merge, _readable
from config import REVIEWS

raw = [
    {"stars": "5 stars", "text": "  They came out at 11pm during the storm and fixed it.  ",
     "when": "2 months ago", "owner_replied": True},
    {"stars": "1 star", "text": "Nice!", "when": "a week ago", "owner_replied": False},   # too short
    {"stars": "2 stars", "text": "Called\x00 three times\x07 and nobody ever answered the phone.",
     "when": "3 days ago", "owner_replied": False},
]
out = _clean(raw)
check("short junk reviews dropped", len(out) == 2, str(out))
check("whitespace trimmed", out[0]["text"].startswith("They came out"))
check("stars parsed to float", out[0]["stars"] == 5.0 and out[1]["stars"] == 2.0)
check("owner reply flag kept", out[0]["owner_replied"] is True)
check("control characters stripped",
      "\x00" not in out[1]["text"] and "\x07" not in out[1]["text"])
long_one = [{"stars": "5 stars", "text": "x" * 5000, "when": None, "owner_replied": False}]
check("pathological review bounded",
      len(_clean(long_one)[0]["text"]) == REVIEWS["max_chars_each"])
check("empty input is safe", _clean([]) == [])

# Quotes are stored WHOLE. A 400-char cap once cut them mid-word ("gave us a
# very w"), which makes a quote useless to an email. The token budget belongs
# at the prompt, not in the database.
real_length = 900
verbatim = [{"stars": "1 star", "when": None, "owner_replied": False,
             "text": "They never answered. " * (real_length // 21)}]
kept = _clean(verbatim)[0]["text"]
check("long review stored verbatim, not chopped at 400",
      len(kept) > 400 and kept.endswith("answered."), f"len={len(kept)}")
many = [{"stars": "5 stars", "text": "y" * 400, "when": None, "owner_replied": False}] * 20
check("no total budget applied at capture", len(_clean(many)) == 20)

# ---------- dates: "2 months ago" has to become a real date ----------
from datetime import date
from core.dates import parse_relative, months_since, is_recent

TODAY = date(2026, 8, 11)
check("months parsed", parse_relative("3 months ago", TODAY) == date(2026, 5, 13))
check("'a week ago' means one", parse_relative("a week ago", TODAY) == date(2026, 8, 4))
check("days parsed", parse_relative("2 days ago", TODAY) == date(2026, 8, 9))
check("years parsed", parse_relative("a year ago", TODAY) == date(2025, 8, 11))
check("sub-day rounds to today", parse_relative("an hour ago", TODAY) == TODAY)
check("yesterday handled", parse_relative("yesterday", TODAY) == date(2026, 8, 10))
check("nonsense returns None", parse_relative("last tuesday", TODAY) is None)
check("empty returns None", parse_relative("", TODAY) is None)
check("months_since counts back", months_since(date(2026, 5, 13), TODAY) == 3)
check("recent evidence passes", is_recent(date(2026, 5, 13), 12, TODAY))
check("stale evidence fails", not is_recent(date(2024, 1, 1), 12, TODAY))
check("unknown date is never 'recent'", not is_recent(None, 12, TODAY))
dated = _clean([{"stars": "1 star", "text": "Nobody ever answered the phone here.",
                 "when": "2 months ago", "owner_replied": False}])
check("capture stamps an absolute date", dated[0]["date"] is not None)
check("...and keeps the original string", dated[0]["when"] == "2 months ago")

# ---------- buckets: complaints AND praise, by star rating ----------
from scraper.reviews import _take

pool = _clean([{"stars": f"{s} stars", "when": None, "owner_replied": False,
                "text": f"A review rated {s} stars, long enough to survive."}
               for s in (5, 1, 4, 2, 5, 3, 5, 1)])
negatives = _take(pool, lambda r: r["stars"] <= 3, 6)
positives = _take(pool, lambda r: r["stars"] >= 4, 5)
check("negatives bucket picks 1-3 star only",
      all(r["stars"] <= 3 for r in negatives) and len(negatives) == 4, str(negatives))
check("positives bucket picks 4-5 star only",
      all(r["stars"] >= 4 for r in positives) and len(positives) == 4)
check("bucket honours its limit", len(_take(pool, lambda r: True, 2)) == 2)
check("shortfall returns what exists, not an error",
      len(_take(pool, lambda r: r["stars"] == 2, 6)) == 1)
check("empty pool is safe", _take([], lambda r: True, 5) == [])

# ---------- merge policy: complaints must outrank praise ----------
# Order is the whole guarantee. The prompt budget trims the tail of this list,
# so complaints have to sit at the head or a flood of five-star praise pushes
# the evidence out of the email.
complaint = {"stars": "1 star", "owner_replied": False, "when": None,
             "text": "Called four times about a filling and nobody ever answered."}
praise = [{"stars": "5 stars", "owner_replied": False, "when": None,
           "text": f"Absolutely wonderful visit number {i}, " + "lovely staff. " * 30}
          for i in range(8)]
merged = _clean(_merge([complaint], praise))
check("low-rated review survives the char budget",
      any("nobody ever answered" in r["text"] for r in merged), str(merged)[:120])
check("...and comes first", merged[0]["stars"] == 1.0)
check("praise still included when there's room", len(merged) > 1)

dupe = dict(complaint)
check("duplicate reviews collapse", len(_merge([complaint], [dupe])) == 1)
check("merge keeps first-list order",
      _merge([complaint], praise)[0]["text"] == complaint["text"])
check("merge drops empty text", _merge([{"text": "  "}], [complaint]) == [complaint])
check("merge of two empties is safe", _merge([], []) == [])
check("_readable counts only cards with text",
      _readable([{"text": "hello there"}, {"text": ""}, {}]) == 1)

# ---------- qualifier signals ----------
from qualifier.qualify import review_signals, score_lead, fit_service, qualify

pain_row = {"reviews_text": json.dumps([
    {"text": "I called three times and nobody ever answered.", "owner_replied": False},
    {"text": "Great work, very tidy.", "owner_replied": True},
])}
sig = review_signals(pain_row)
check("pain phrase detected", sig["pain_hits"] == 1, str(sig))
check("owner reply counted", sig["owner_replies"] == 1, str(sig))
check("review count reported", sig["count"] == 2)

for phrase in ["never called back", "couldn't get through", "went to voicemail",
               "left several messages", "no one answered", "still waiting for a call"]:
    r = {"reviews_text": json.dumps([{"text": f"Terrible, {phrase} at all.", "owner_replied": False}])}
    check(f"pain phrase '{phrase[:22]}' detected", review_signals(r)["pain_hits"] == 1)

happy = {"reviews_text": json.dumps([{"text": "Absolutely brilliant service, very quick.",
                                      "owner_replied": False}])}
check("praise is NOT a pain hit", review_signals(happy)["pain_hits"] == 0)
check("no reviews -> all zeros", review_signals({})["pain_hits"] == 0)
check("corrupt JSON -> all zeros", review_signals({"reviews_text": "{bad"})["count"] == 0)

# scoring: pain adds points, and leads without reviews are unchanged
base = {"email_status": "valid", "phone": "1", "website": "http://x.com",
        "rating": 4.5, "reviews": 50, "open_state": "Open"}
with_pain = {**base, "reviews_text": json.dumps(
    [{"text": "Nobody ever answered the phone.", "owner_replied": True}])}
check("review pain raises the score", score_lead(with_pain) > score_lead(base),
      f"{score_lead(base)} -> {score_lead(with_pain)}")
check("no-review lead scores exactly as before", score_lead(base) == score_lead({**base}))
fit, reason = fit_service(with_pain)
check("pain drives the fit reason", "review" in (reason or "").lower(), reason)

# ---------- storage round-trip ----------
from storage import store
from core.models import Lead
tmp = os.path.join(tempfile.gettempdir(), "reviews_test.db")
if os.path.exists(tmp): os.remove(tmp)
conn = store.connect(tmp)
now = "2026-07-27T10:00:00"
revs = [{"stars": 5.0, "text": "They answered at 11pm during the storm.",
         "when": "2 months ago", "owner_replied": True}]
lead = Lead(place_key="k1", name="Storm Roofing", category="Roofer", address="a",
            phone="1", website="http://x.com", rating=4.8, reviews=62,
            maps_url="u", query="roofers in Dallas, TX", reviews_text=revs)
store.save_lead(conn, lead, now, vertical="commercial_contractor")
row = store.lead_row(conn, "k1")
stored = json.loads(row["reviews_text"])
check("reviews survive the DB round-trip", stored[0]["text"] == revs[0]["text"])
check("owner_replied survives", stored[0]["owner_replied"] is True)

# a lead saved WITHOUT reviews must not blow up or wipe existing ones
plain = Lead(place_key="k2", name="No Reviews Co", category="X", address="a",
             phone=None, website=None, rating=None, reviews=None,
             maps_url="u", query="q")
store.save_lead(conn, plain, now)
check("lead without reviews saves fine", store.lead_row(conn, "k2")["reviews_text"] is None)
store.save_lead(conn, Lead(place_key="k1", name="Storm Roofing", category="Roofer",
                           address="a", phone="1", website="http://x.com", rating=4.8,
                           reviews=62, maps_url="u", query="q"), now)
check("re-scrape without reviews does NOT wipe stored ones",
      store.lead_row(conn, "k1")["reviews_text"] is not None)
conn.close(); os.remove(tmp)

# ---------- LLM prompt ----------
from enrichment.llm_writer import _lead_brief, _review_lines, _prompt
brief = _lead_brief({"name": "Storm Roofing", "category": "Roofer",
                     "query": "roofers in Dallas, TX", "rating": 4.8, "reviews": 62,
                     "reviews_text": json.dumps(revs)}, "site text here")
check("reviews reach the prompt brief", "11pm during the storm" in brief)
check("brief labels the review section", "customers say" in brief.lower())
check("prompt forbids naming reviewers", "NEVER name or" in _prompt())
check("no reviews -> brief still valid",
      "customers say" not in _lead_brief({"name": "X"}, "site").lower())
check("review lines cap on total length",
      sum(len(x) for x in _review_lines({"reviews_text": json.dumps(
          [{"text": "z" * 400}] * 30)})) <= 4000)

print(f"\nALL {passed} REVIEW CHECKS PASSED")
