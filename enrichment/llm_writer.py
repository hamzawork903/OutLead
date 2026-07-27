"""
LLM email writer — writes the ENTIRE 3-step sequence for one lead, from that
lead's real data (name, trade, city, rating, website text). No hardcoded copy:
the model writes it, this module validates it, storage keeps it, and the send
path only ever sends what passed validation.

Fail-safe: no API key, disabled, thin data, API error, or invalid output all
return None — the lead simply has no generated emails yet, and the send path
skips it loudly instead of sending something unreviewed.

Needs OPENAI_API_KEY in .env (loaded by config.py's dotenv call).
"""

import json
import os
import re
import threading

import httpx

from config import LLM, LOGS_DIR, OUTREACH_IDENTITY, OUTREACH_OFFER
from core.logbook import get_logger

log = get_logger(__name__)

_API_URL = "https://api.openai.com/v1/chat/completions"

# ---- hard spending cap (user rule: $5 max for the test phase) -------------
# Spend is estimated from the API's own usage counts per call and persisted,
# so it survives restarts and accumulates across scrape/enrich/backfill runs.
_SPEND_PATH = LOGS_DIR / "llm_spend.json"
_spend_lock = threading.Lock()


def _spent_usd() -> float:
    try:
        return float(json.loads(_SPEND_PATH.read_text())["spent_usd"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 0.0


def _record_spend(prompt_tokens: int, completion_tokens: int) -> float:
    price = LLM["price_per_mtok"]
    cost = (prompt_tokens / 1e6 * price["input"]
            + completion_tokens / 1e6 * price["output"])
    with _spend_lock:
        data = {"spent_usd": 0.0, "calls": 0}
        try:
            data.update(json.loads(_SPEND_PATH.read_text()))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        data["spent_usd"] = round(data["spent_usd"] + cost, 6)
        data["calls"] = data.get("calls", 0) + 1
        LOGS_DIR.mkdir(exist_ok=True)
        _SPEND_PATH.write_text(json.dumps(data))
    return data["spent_usd"]

PROMPT = """You write short cold-outreach emails for {business_name}, which \
sells: {offer}

You get real data about ONE business. Write a 3-step email sequence to its owner.

VOICE: a real person being helpful. Simple everyday words a busy owner reads \
in ten seconds. Short sentences. No jargon, no hype, no exclamation marks, no \
emojis, never salesy or desperate.

BE SPECIFIC: every email must mention at least one true detail from THEIR data \
(their trade, city, specialty, hours, or how customers reach them) so it could \
not have been sent to any other business. Never invent facts. If the website \
text is thin, lean on their trade and city.

THE ANGLE (the pain to lead with): {angle}

STEPS:
1. pitch — a specific observation about their business, the problem above put \
as a short question, what the offer would do for THEM, then invite them to \
pick a time at [BOOK_LINK].
2. nudge (sent a few days later) — very short, one fresh angle on the same \
problem, remind them of [BOOK_LINK].
3. goodbye (sent about a week later) — polite close-out, zero pressure, door \
stays open at [BOOK_LINK].

HARD RULES:
- each body: 50-120 words, plain text, 2-4 short paragraphs
- dashes: use a dash as punctuation AT MOST ONCE per email, and only if truly \
needed. Prefer commas and full stops. (Hyphens inside words like "family-owned" \
or "24-hour" are fine.)
- the ONLY link allowed is the placeholder [BOOK_LINK] — never a real URL or \
email address
- no greeting (no "Hi ...") — the system adds it
- no signature, no unsubscribe line — the system adds them
- subjects: 2-5 words, all lowercase, plain, like a note from a colleague
- greeting_name: the business's natural short name for "Hi <name>," (e.g. \
"Baker Roofing" from "Baker Roofing & Construction, Inc"); null if awkward

Return ONLY this JSON, nothing else:
{{"greeting_name": "..." , "steps": [{{"subject": "...", "body": "..."}}, \
{{"subject": "...", "body": "..."}}, {{"subject": "...", "body": "..."}}]}}"""

_BANNED_RE = re.compile(r"https?://|www\.|@|unsubscribe|as an ai|language model",
                        re.IGNORECASE)
_NAME_OK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'.\-]{1,39}$")

_warned_no_key = False


def _prompt() -> str:
    """The system prompt, filled with THIS operator's offer (config/.env) —
    nothing about the pitch is hardcoded, so a fresh clone sells its own
    product, not the author's."""
    return PROMPT.format(
        business_name=OUTREACH_IDENTITY["business_name"],
        offer=OUTREACH_OFFER["what_you_sell"],
        angle=OUTREACH_OFFER["angle"],
    )


def _clean(text: str) -> str:
    """Normalize whitespace and strip format-breaking braces (LLM output is
    untrusted input on its way into a real email)."""
    text = (text or "").replace("{", "").replace("}", "").replace("[ BOOK_LINK ]", "[BOOK_LINK]")
    return re.sub(r"[ \t]+", " ", text).strip()


def _valid_step(step: dict) -> dict | None:
    subject = _clean(step.get("subject", "")).lower().replace("\n", " ")
    body = _clean(step.get("body", ""))
    if not subject or len(subject) > 48:
        return None
    words = len(body.split())
    if not (30 <= words <= 160):
        return None
    if "[BOOK_LINK]" not in body:
        return None
    if _BANNED_RE.search(subject) or _BANNED_RE.search(body.replace("[BOOK_LINK]", "")):
        return None
    if body.lower().startswith(("hi", "hello", "hey", "dear")):
        return None                       # greeting is ours to add
    # Dash discipline (user rule + classic AI tell): at most ONE punctuation
    # dash per body. Hyphens inside words (family-owned, 24-hour) don't count.
    dash_count = body.count("—") + body.count("–") + body.count(" - ")
    if dash_count > 1:
        return None
    return {"subject": subject, "body": body}


def validate(raw: str) -> dict | None:
    """Parse + validate the model's JSON. Returns {greeting_name, steps[3]}
    or None if ANYTHING is off — we never send unvalidated copy."""
    text = raw.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON — discarding")
        return None

    steps_in = data.get("steps") or []
    if len(steps_in) != 3:
        log.warning("LLM returned %d steps (need 3) — discarding", len(steps_in))
        return None
    steps = []
    for i, s in enumerate(steps_in):
        ok = _valid_step(s if isinstance(s, dict) else {})
        if ok is None:
            log.warning("LLM step %d failed validation — discarding all", i)
            return None
        steps.append(ok)

    name = _clean(str(data.get("greeting_name") or ""))
    if not _NAME_OK_RE.match(name):
        name = None                        # falls back to a bare "Hi,"
    return {"greeting_name": name, "steps": steps}


def _lead_brief(lead: dict, site_text: str) -> str:
    parts = [f"Business name: {lead.get('name') or 'unknown'}",
             f"Type: {lead.get('category') or 'local business'}"]
    query = lead.get("query") or ""
    if " in " in query:
        parts.append(f"Area: {query.split(' in ', 1)[1]}")
    if lead.get("rating"):
        parts.append(f"Google rating: {lead['rating']} ({lead.get('reviews') or 0} reviews)")
    parts.append("\nWebsite text:\n" + (site_text or "")[:LLM["max_site_chars"]])
    return "\n".join(parts)


def compose_emails(lead: dict, site_text: str) -> str | None:
    """Generate + validate the full sequence for one lead. Returns the JSON
    string to store (leads.llm_emails), or None (= not generated yet)."""
    global _warned_no_key
    key = os.environ.get("OPENAI_API_KEY")
    if not LLM["enabled"] or not key:
        if not key and LLM["enabled"] and not _warned_no_key:
            _warned_no_key = True
            log.info("  LLM email writing off: OPENAI_API_KEY not set in .env")
        return None
    spent = _spent_usd()
    if spent >= LLM["budget_usd"]:
        log.error("LLM NOT WORKING: the $%.2f test budget is used up "
                  "($%.4f spent). No more emails will be generated until "
                  "LLM['budget_usd'] is raised in config.py.",
                  LLM["budget_usd"], spent)
        return None
    if not site_text or len(site_text) < LLM["min_site_chars"]:
        site_text = ""                     # thin site: model leans on trade+city

    body = {
        "model": LLM["model"],
        "max_tokens": 900,
        "temperature": 0.5,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _prompt()},
            {"role": "user", "content": _lead_brief(lead, site_text)},
        ],
    }
    try:
        resp = httpx.post(_API_URL, json=body, timeout=LLM["timeout_s"],
                          headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            log.warning("LLM call failed (HTTP %d) for %r", resp.status_code,
                        lead.get("name"))
            return None
        payload = resp.json()
        raw = payload["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as err:
        log.warning("LLM call failed (%s) for %r", type(err).__name__,
                    lead.get("name"))
        return None

    usage = payload.get("usage") or {}
    total = _record_spend(usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0))

    result = validate(raw)
    if result is None:
        return None
    log.info("  emails written for %r (greeting: %s) | LLM spend so far: $%.4f",
             lead.get("name"), result["greeting_name"] or "none", total)
    return json.dumps(result)
