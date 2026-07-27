"""
Email rendering — no hardcoded copy. Every lead's 3-step sequence is written
by the LLM at enrichment time (enrichment/llm_writer.py), validated, and
stored as JSON on leads.llm_emails. This module only turns that stored copy
into a sendable email:

  greeting  "Hi <greeting_name>," (LLM-cleaned, validated) or a bare "Hi,"
  body      the stored step body, [BOOK_LINK] -> the real Calendly URL
  footer    our identity + opt-out line (formal tone for insurance/law_pi)
  html      the same content in the minimal wrapper (email_template.html)

A lead with no stored copy renders None — the send path skips it loudly
rather than sending anything unwritten/unvalidated.
"""

import html as html_lib
import json
from pathlib import Path

from config import OUTREACH_IDENTITY

_FORMAL_VERTICALS = {"insurance", "law_pi"}
_REPLY_NOTE_FORMAL = ("If now isn't the right time, reply UNSUBSCRIBE and "
                      "we'll take you off our list right away.")

_HTML_TEMPLATE_PATH = Path(__file__).with_name("email_template.html")
_html_template_cache: str | None = None


def _footer_note(service_fit) -> str:
    return (_REPLY_NOTE_FORMAL if service_fit in _FORMAL_VERTICALS
            else OUTREACH_IDENTITY["reply_note"])


def _load(lead: dict) -> dict | None:
    raw = lead.get("llm_emails")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if len(data.get("steps") or []) == 3 else None
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def has_generated(lead: dict) -> bool:
    """True when this lead has a validated LLM-written sequence to send."""
    return _load(lead) is not None


def render_email(service_fit, step: int, lead: dict):
    """(subject, text_body, html_body) for one step — or None if this lead
    has no generated sequence yet (caller skips, never substitutes copy)."""
    data = _load(lead)
    if data is None:
        return None
    step_data = data["steps"][min(step, 2)]

    name = data.get("greeting_name")
    greeting = f"Hi {name}," if name else "Hi,"
    calendly = OUTREACH_IDENTITY["calendly_url"]
    body_core = step_data["body"].replace("[BOOK_LINK]", calendly)

    text = (f"{greeting}\n\n{body_core}\n\n"
            f"{OUTREACH_IDENTITY['from_name']}\n"
            f"{OUTREACH_IDENTITY['title']}, {OUTREACH_IDENTITY['business_name']}\n\n"
            f"{_footer_note(service_fit)}")
    html = _render_html(greeting, step_data["body"], calendly,
                        _footer_note(service_fit))
    return step_data["subject"], text, html


def _render_html(greeting: str, body_raw: str, calendly: str, note: str) -> str:
    """Fill the minimal HTML wrapper. All copy is escaped; [BOOK_LINK] becomes
    a real anchor only AFTER escaping, so no LLM text can inject markup."""
    global _html_template_cache
    if _html_template_cache is None:
        _html_template_cache = _HTML_TEMPLATE_PATH.read_text(encoding="utf-8")

    link = (f'<a href="{calendly}" '
            f'style="color:#ff3131;font-weight:600;">pick a time here</a>')
    paragraphs = []
    for para in body_raw.split("\n"):
        para = para.strip()
        if not para:
            continue
        safe = html_lib.escape(para).replace("[BOOK_LINK]", link)
        paragraphs.append(
            f'<p style="margin:0 0 16px 0;line-height:1.6;">{safe}</p>')

    return (_html_template_cache
            .replace("{greeting}", html_lib.escape(greeting))
            .replace("{paragraphs}", "\n".join(paragraphs))
            .replace("{button_url}", calendly)
            .replace("{from_name}", html_lib.escape(OUTREACH_IDENTITY["from_name"]))
            .replace("{title}", html_lib.escape(OUTREACH_IDENTITY["title"]))
            .replace("{business_name}", html_lib.escape(OUTREACH_IDENTITY["business_name"]))
            .replace("{website}", html_lib.escape(OUTREACH_IDENTITY["website"]))
            .replace("{reply_note}", html_lib.escape(note)))
