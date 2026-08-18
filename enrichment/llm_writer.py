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
import re

from config import LLM, OUTREACH_IDENTITY, OUTREACH_OFFER
from core import llm
from core.logbook import get_logger

log = get_logger(__name__)

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
- if customer reviews are supplied, use them for a true, specific observation \
about the business, but NEVER quote a review word-for-word and NEVER name or \
refer to an individual reviewer. Write it as something you noticed.
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


def _review_lines(lead: dict) -> list:
    """Customer reviews as prompt lines. This is the strongest personalisation
    material we have — what customers SAY beats what the business claims about
    itself — but it's also user-generated text from strangers, so it's
    control-stripped at capture (scraper/reviews.py) and the prompt forbids
    quoting reviewer names. Reviews arrive complaints-first, so the budget
    below trims praise rather than the evidence."""
    raw = lead.get("reviews_text")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not raw:
        return []
    lines, used = [], 0
    for r in raw[:LLM["review_count"]]:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        if used + len(text) > LLM["review_chars_total"]:
            break
        used += len(text)
        stars = r.get("stars")
        when = r.get("when")
        tag = f"{stars}/5" if stars else "review"
        if when:
            tag += f", {when}"
        lines.append(f"- ({tag}) {text}")
    return lines


def _lead_brief(lead: dict, site_text: str) -> str:
    parts = [f"Business name: {lead.get('name') or 'unknown'}",
             f"Type: {lead.get('category') or 'local business'}"]
    query = lead.get("query") or ""
    if " in " in query:
        parts.append(f"Area: {query.split(' in ', 1)[1]}")
    if lead.get("rating"):
        parts.append(f"Google rating: {lead['rating']} ({lead.get('reviews') or 0} reviews)")

    reviews = _review_lines(lead)
    if reviews:
        parts.append("\nWhat their customers say (use these for a specific, "
                     "true observation — never name or quote a reviewer):\n"
                     + "\n".join(reviews))
    parts.append("\nWebsite text:\n" + (site_text or "")[:LLM["max_site_chars"]])
    return "\n".join(parts)


def compose_emails(lead: dict, site_text: str) -> str | None:
    """Generate + validate the full sequence for one lead. Returns the JSON
    string to store (leads.llm_emails), or None (= not generated yet)."""
    if not site_text or len(site_text) < LLM["min_site_chars"]:
        site_text = ""                     # thin site: model leans on trade+city

    raw = llm.ask_json(_prompt(), _lead_brief(lead, site_text),
                       max_tokens=900, label=repr(lead.get("name")))
    if raw is None:
        return None

    result = validate(raw)
    if result is None:
        return None
    log.info("  emails written for %r (greeting: %s) | LLM spend so far: $%.4f",
             lead.get("name"), result["greeting_name"] or "none", llm.spent_usd())
    return json.dumps(result)
