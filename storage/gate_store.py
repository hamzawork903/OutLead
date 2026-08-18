"""
Reads and writes for the gate. Split out of store.py along the same seam as
campaigns.py — store.py is well over the size cap, and these queries are one
concern: what the gate needs in, and what it records out.

Gate results are never destructive. A dropped lead keeps its row and gains a
reason, so changing a threshold re-runs against yesterday's rejects for free
instead of needing another hour of scraping.
"""

import json

from core.logbook import get_logger

log = get_logger(__name__)

_GATE_COLUMNS = ("gate_status", "drop_reason", "door", "tier", "priority_score",
                 "problem_type", "problem_summary", "gap", "offer",
                 "evidence_count", "quality_ratio",
                 "praise_point", "quotes", "judge_verdict", "gated_at")


def leads_for_vertical(conn, slug: str) -> list:
    """Every stored lead of one vertical, as plain dicts."""
    cur = conn.execute("SELECT * FROM leads WHERE vertical = ?", (slug,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def leads_for_query(conn, query: str) -> list:
    """Every stored lead from one scrape query — used when leads were scraped
    before a vertical was assigned."""
    cur = conn.execute("SELECT * FROM leads WHERE query = ?", (query,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def suppressed_emails(conn) -> set:
    """Every address that must never be contacted again."""
    return {row[0].lower() for row in
            conn.execute("SELECT email FROM suppressions") if row[0]}


def save_gate_result(conn, result: dict) -> None:
    """Record one lead's gate verdict. Additive — nothing else on the row is
    touched, so a re-run can't lose enrichment or review text."""
    sets = ", ".join(f"{col} = ?" for col in _GATE_COLUMNS)
    values = [result.get(col) for col in _GATE_COLUMNS]
    conn.execute(f"UPDATE leads SET {sets} WHERE place_key = ?",
                 [*values, result["place_key"]])
    conn.commit()


def gated_leads(conn, vertical: str, status: str = "lead") -> list:
    """Gated leads of one vertical, best first. `status` is 'lead' or
    'dropped'."""
    cur = conn.execute(
        "SELECT * FROM leads WHERE vertical = ? AND gate_status = ? "
        "ORDER BY priority_score DESC, reviews DESC", (vertical, status))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def all_dropped(conn) -> list:
    """Everything the gate rejected, across every vertical."""
    cur = conn.execute(
        "SELECT * FROM leads WHERE gate_status = 'dropped' "
        "ORDER BY gated_at DESC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def save_website_text(conn, place_key: str, text: str) -> None:
    """Keep the homepage text the harvester already fetched.

    Without it the gate can't check accreditations or an out-of-hours claim
    without going back to the network, which is what makes a re-run instant."""
    if not text:
        return
    conn.execute("UPDATE leads SET website_text = ? WHERE place_key = ?",
                 (text, place_key))
    conn.commit()


def sheet_rows(leads: list) -> list:
    """Gate results flattened for the spreadsheet: quotes unpacked into their
    own columns so you can read the evidence without opening JSON.

    Each row is labelled with the lead's OWN vertical. Stamping them all with
    whichever vertical happened to be running relabelled 45 dentists as
    plumbers the first time this ran — the _Dropped tab holds every vertical at
    once, so it can't inherit one."""
    from qualifier import profiles
    rows = []
    for lead in leads:
        try:
            quotes = json.loads(lead.get("quotes") or "[]")
        except json.JSONDecodeError:
            quotes = []
        row = {
            "scraped_at": lead.get("first_seen"),
            "vertical": profiles.name_for(lead.get("vertical") or ""),
            "city": (lead.get("query") or "").split(" in ")[-1],
            "business_name": lead.get("name"),
            "category": lead.get("category"),
            "rating": lead.get("rating"),
            "review_count": lead.get("reviews"),
            "phone": lead.get("phone"),
            "email": lead.get("email"),
            "email_status": lead.get("email_status"),
            "website": lead.get("website"),
            "address": lead.get("address"),
            "door": lead.get("door"),
            "tier": lead.get("tier"),
            "priority_score": lead.get("priority_score"),
            "gap": lead.get("gap"),
            "offer": lead.get("offer"),
            "problem_type": lead.get("problem_type"),
            "problem_summary": lead.get("problem_summary"),
            "evidence_count": lead.get("evidence_count"),
            "quality_ratio": lead.get("quality_ratio"),
            "praise_point": lead.get("praise_point"),
            "gate_status": lead.get("gate_status"),
            "drop_reason": lead.get("drop_reason"),
            "maps_url": lead.get("maps_url"),
            "place_key": lead.get("place_key"),
        }
        for i, quote in enumerate(quotes[:2], start=1):
            row[f"quote_{i}_text"] = quote.get("text")
            row[f"quote_{i}_date"] = quote.get("date")
            row[f"quote_{i}_stars"] = quote.get("stars")
        rows.append(row)
    return rows
