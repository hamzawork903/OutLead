"""
Campaign-view queries + settings for the Outreach dashboard. A "campaign" is
one scrape query ("dentists in Birmingham, UK") — every lead remembers the
query that found it, so grouping by it gives the campaign view for free.

Split from store.py along the dashboard seam (store.py is over the size cap);
same rule applies here: this is the only place these queries live.
"""

import json

from config import SEQUENCE
from core.logbook import get_logger

log = get_logger(__name__)


# ------------------------------------------------------------- settings ----

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default


def set_setting(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                 (key, json.dumps(value)))
    conn.commit()


def step_delays(conn) -> list:
    """The sequence timing (day offsets, first is always 0). DB-set value
    wins; config.SEQUENCE['step_delay_days'] is the fallback default."""
    delays = get_setting(conn, "step_delay_days")
    if (isinstance(delays, list) and delays and delays[0] == 0
            and all(isinstance(d, int) and d >= 0 for d in delays)
            and delays == sorted(delays)):
        return delays
    return list(SEQUENCE["step_delay_days"])


def save_step_delays(conn, delays: list) -> None:
    set_setting(conn, "step_delay_days", delays)
    log.info("sequence timing saved: %s", delays)


# ------------------------------------------------------------ campaigns ----

def campaigns(conn, now) -> list:
    """One row per campaign (scrape query) that has sequences: counts by
    status, how many are sendable right now, and when it started."""
    cur = conn.execute(
        """SELECT l.query AS query,
                  MAX(COALESCE(l.vertical, '')) AS vertical,
                  COUNT(*) AS total,
                  SUM(CASE WHEN s.status='active' THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN s.status='replied' THEN 1 ELSE 0 END) AS replied,
                  SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN s.status='stopped' THEN 1 ELSE 0 END) AS stopped,
                  SUM(CASE WHEN s.first_sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent_any,
                  SUM(CASE WHEN s.status='active' AND s.next_send_at <= ?
                           THEN 1 ELSE 0 END) AS due_now,
                  MIN(s.started_at) AS created_at
           FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
           GROUP BY l.query ORDER BY MIN(s.started_at) DESC""",
        (now,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def campaign_leads(conn, query) -> list:
    """Every sequenced lead of one campaign, for the campaign detail table."""
    cur = conn.execute(
        """SELECT s.place_key, l.name, l.email, l.email_status,
                  l.quality_score, s.current_step, s.status, s.stop_reason,
                  s.next_send_at, s.first_sent_at,
                  (l.llm_emails IS NOT NULL) AS has_emails
           FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
           WHERE l.query = ?
           ORDER BY l.quality_score DESC""",
        (query,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def lead_detail(conn, place_key) -> dict | None:
    """Everything the lead drawer shows: the lead, its generated emails
    (parsed), its sequence state, and its full send history."""
    cur = conn.execute(
        """SELECT l.place_key, l.name, l.email, l.email_status, l.website,
                  l.category, l.rating, l.reviews, l.quality_score, l.query,
                  l.vertical, l.llm_emails, l.reviews_text,
                  s.current_step, s.status, s.stop_reason, s.next_send_at
           FROM leads l LEFT JOIN outreach_sequences s
                ON s.place_key = l.place_key
           WHERE l.place_key = ?""", (place_key,))
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    lead = dict(zip(cols, row))
    try:
        lead["emails"] = json.loads(lead.pop("llm_emails") or "null")
    except json.JSONDecodeError:
        lead["emails"] = None
    hist = conn.execute(
        """SELECT step, email, subject, sent_at, status, dry_run, error
           FROM outreach_log WHERE place_key = ? ORDER BY sent_at DESC""",
        (place_key,))
    hcols = [d[0] for d in hist.description]
    lead["history"] = [dict(zip(hcols, r)) for r in hist.fetchall()]
    return lead


def attention(conn, limit=30) -> list:
    """Replied and stopped sequences, newest first — the 'needs attention'
    feed. A reply is the whole point of the system; never let one sit unseen."""
    cur = conn.execute(
        """SELECT s.place_key, l.name, l.email, s.status, s.stop_reason,
                  s.updated_at, l.query
           FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
           WHERE s.status IN ('replied', 'stopped')
           ORDER BY s.updated_at DESC LIMIT ?""", (limit,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
