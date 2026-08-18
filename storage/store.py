"""
The lead store. Now three tables:

  leads  — the durable record. `place_key` PRIMARY KEY = automatic dedupe.
           `status` tracks pipeline stage (extracted -> enriched later).
  queue  — the crash-resume work list. Collection enqueues listings as
           'pending'; extraction flips each to 'done' (or 'failed'). A run that
           dies mid-way leaves 'pending' rows, so restarting continues exactly
           where it stopped instead of re-scraping from zero.
  run_log — one row per run (when, query, how many). Powers rate discipline:
           the daily cap and the between-run cooldown.

Plus CSV export of a run's leads (Excel-friendly, contact fields first).
"""

import csv
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime

from config import DB_PATH, EXPORTS_DIR, SEQUENCE
from core.logbook import get_logger
from core.models import Listing

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    place_key   TEXT PRIMARY KEY,
    name TEXT, category TEXT, address TEXT, phone TEXT, website TEXT,
    rating REAL, reviews INTEGER, maps_url TEXT, query TEXT,
    status TEXT DEFAULT 'extracted', first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    query TEXT, place_key TEXT, name TEXT, url TEXT,
    status TEXT DEFAULT 'pending',   -- pending | done | failed
    enqueued_at TEXT,
    PRIMARY KEY (query, place_key)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT, started_at TEXT, ended_at TEXT,
    extracted INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS suppressions (
    email TEXT PRIMARY KEY,
    reason TEXT,          -- unsubscribe_reply | bounced | manual
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS outreach_sequences (
    place_key TEXT PRIMARY KEY,
    service_fit TEXT,                     -- which template track
    current_step INTEGER DEFAULT 0,       -- 0 = not started yet
    status TEXT DEFAULT 'active',         -- active | replied | stopped | completed
    stop_reason TEXT,                     -- unsubscribed | replied | bounced | suppressed
    next_send_at TEXT,
    started_at TEXT,                      -- when enqueued
    first_sent_at TEXT,                   -- when step 0 actually sent (anchors delays)
    last_sent_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_key TEXT, step INTEGER, email TEXT, subject TEXT,
    sent_at TEXT, status TEXT, dry_run INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT          -- JSON-encoded; reads fall back to config defaults
);
"""

_SOCIAL_COLS = ["facebook", "instagram", "linkedin", "youtube", "tiktok", "twitter"]

_PLACE_COLS = ["open_state", "hours", "price_level", "plus_code"]

_QUALIFY_COLS = ["quality_score", "service_fit", "fit_reason", "qualify_status"]

_CSV_COLUMNS = (["name", "category", "phone", "website_phones", "website",
                 "email", "email_status", "all_emails"]
                + _SOCIAL_COLS
                + ["address", "rating", "reviews", "reviews_text",
                   "open_state", "hours", "price_level", "plus_code"]
                + _QUALIFY_COLS
                + ["query", "maps_url"])

# Columns added after the leads table first shipped. Applied via additive
# migration so existing databases upgrade without losing any data.
#   M5: email, all_emails, email_source
#   M7: social profile links
#   M8: website_phones
#   M9: open_state, hours, price_level, plus_code
#   M11: email_status (valid | risky | invalid | unknown)
#   Phase 2 stage 1: quality_score, service_fit, fit_reason, qualify_status
#   Phase 2 GTM: vertical (scrape-time business vertical -> email routing key)
_MIGRATIONS = ([
    ("email", "TEXT"),
    ("all_emails", "TEXT"),
    ("email_source", "TEXT"),
] + [(col, "TEXT") for col in _SOCIAL_COLS]
  + [("website_phones", "TEXT")]
  + [(col, "TEXT") for col in _PLACE_COLS]
  + [("email_status", "TEXT")]
  + [("quality_score", "INTEGER"), ("service_fit", "TEXT"),
     ("fit_reason", "TEXT"), ("qualify_status", "TEXT")]
  + [("vertical", "TEXT")]
  + [("personal_line", "TEXT")]    # legacy (superseded by llm_emails)
  + [("llm_emails", "TEXT")]       # JSON: LLM-written 3-step sequence + greeting
  + [("reviews_text", "TEXT")]     # JSON: captured Google reviews (opt-in group)
  # Homepage text, kept so the gate can check accreditations and out-of-hours
  # claims without going back to the network. That's what makes a gate re-run
  # on stored leads instant and free.
  + [("website_text", "TEXT")]
  # Gate results. `door` is the one to read: 1 means a customer said they have
  # the problem, 2 means only their setup suggests it — measured separately or
  # you can never tell which half of the funnel is working.
  + [("gate_status", "TEXT"), ("drop_reason", "TEXT"), ("door", "INTEGER"),
     ("tier", "TEXT"), ("priority_score", "INTEGER"),
     ("problem_type", "TEXT"), ("problem_summary", "TEXT"),
     ("gap", "TEXT"), ("offer", "TEXT"),
     ("evidence_count", "INTEGER"),
     ("quality_ratio", "REAL"), ("praise_point", "TEXT"),
     ("quotes", "TEXT"),          # JSON: the 1-2 quotes with date + stars
     ("judge_verdict", "TEXT"),   # JSON: cached LLM verdict, so re-runs are free
     ("gated_at", "TEXT")])


# Field groups the operator can switch on/off per run. Maps-side groups are
# "free" (one page load); enrichment groups cost a website visit each.
FIELD_GROUPS = {
    "basics":         ["name", "category", "address"],
    "phone_website":  ["phone", "website"],
    "ratings":        ["rating", "reviews"],
    "hours_price":    ["open_state", "hours", "price_level", "plus_code"],
    "emails":         ["email", "email_status", "all_emails"],
    "socials":        _SOCIAL_COLS,
    "website_phones": ["website_phones"],
    "reviews_text":   ["reviews_text"],
}
ENRICH_GROUPS = {"emails", "socials", "website_phones"}   # need a website visit


def groups_from_arg(spec: str | None) -> list[str]:
    """Parse a --fields "a,b,c" spec into valid group keys; default = all."""
    if not spec:
        return list(FIELD_GROUPS)
    picked = [g.strip() for g in spec.split(",") if g.strip() in FIELD_GROUPS]
    if "basics" not in picked:
        picked.insert(0, "basics")            # name/category/address always kept
    return picked or list(FIELD_GROUPS)


def columns_for_groups(groups) -> list[str]:
    """CSV columns for the chosen groups, in the canonical order."""
    wanted = {"name"}
    for g in groups:
        wanted |= set(FIELD_GROUPS.get(g, []))
    cols = [c for c in _CSV_COLUMNS if c in wanted]
    for extra in ("query", "maps_url"):        # always keep for traceability
        if extra not in cols:
            cols.append(extra)
    return cols


def wants_enrichment(groups) -> bool:
    return bool(set(groups) & ENRICH_GROUPS)


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.executescript(_SCHEMA)
    conn.commit()
    migrate(conn)
    return conn


def migrate(conn) -> None:
    """Add any newer columns to an existing leads table. Safe to run every
    time — only missing columns are added; existing data is untouched."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    for column, coltype in _MIGRATIONS:
        if column not in have:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {coltype}")
            log.debug("migration: added leads.%s", column)
    conn.commit()


# --------------------------------------------------------------- leads ----

def save_lead(conn, lead, now: str, vertical=None) -> bool:
    """Insert a lead or refresh last_seen. Returns True if it was NEW.

    `vertical`, when given, tags the lead with the scrape's business vertical
    (e.g. 'insurance', 'dental') — the routing key that later decides WHICH
    email track this lead gets, and which spreadsheet tab it lands in.

    Set ONCE, on the run that first found the business. It used to be last
    write wins, which quietly re-tagged leads: a Manchester plumber turned up
    again in an "emergency electrician" search, and the re-save moved it into
    the electrician vertical — where it would have been pitched the wrong
    product. `query` already behaves this way, and the two describe the same
    event, so they now agree. Re-target deliberately with retag_vertical()."""
    d = asdict(lead)
    # reviews_text is a list of dicts on the dataclass; SQLite takes JSON.
    reviews_json = json.dumps(d.get("reviews_text")) if d.get("reviews_text") else None
    exists = conn.execute(
        "SELECT 1 FROM leads WHERE place_key = ?", (lead.place_key,)
    ).fetchone() is not None
    if exists:
        if vertical:
            # Only fills a blank — never steals a lead from the vertical that
            # found it first.
            conn.execute(
                "UPDATE leads SET last_seen = ?, "
                "vertical = COALESCE(NULLIF(vertical, ''), ?) "
                "WHERE place_key = ?", (now, vertical, lead.place_key))
        else:
            conn.execute("UPDATE leads SET last_seen = ? WHERE place_key = ?",
                         (now, lead.place_key))
        # Re-scrapes can newly carry reviews (the group may have been off the
        # first time). Only overwrite when we actually captured some.
        if reviews_json:
            conn.execute("UPDATE leads SET reviews_text = ? WHERE place_key = ?",
                         (reviews_json, lead.place_key))
        log.debug("dedupe: %r already stored, refreshed last_seen", lead.name)
    else:
        conn.execute(
            """INSERT INTO leads (place_key, name, category, address, phone,
                   website, rating, reviews, maps_url, query, vertical,
                   open_state, hours, price_level, plus_code, reviews_text,
                   status, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'extracted', ?, ?)""",
            (d["place_key"], d["name"], d["category"], d["address"], d["phone"],
             d["website"], d["rating"], d["reviews"], d["maps_url"], d["query"],
             vertical, d["open_state"], d["hours"], d["price_level"],
             d["plus_code"], reviews_json, now, now),
        )
    conn.commit()
    return not exists


# ------------------------------------------------ email enrichment ----

def leads_needing_email(conn, query=None, limit=None) -> list:
    """Leads that have a website but haven't been through enrichment yet.
    Returns (place_key, name, website) tuples. This IS the resume list —
    already-enriched leads are excluded, so a re-run only does what's left."""
    sql = ("SELECT place_key, name, website, category, query, rating, reviews, "
           "reviews_text FROM leads WHERE website IS NOT NULL AND website != '' "
           "AND status = 'extracted'")
    params = []
    if query:
        sql += " AND query = ?"
        params.append(query)
    sql += " ORDER BY reviews DESC"   # richest businesses first
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def save_enrichment(conn, place_key, email, all_emails, source, status,
                    socials, phones, email_status, now) -> None:
    """Record an enrichment result (email + verdict + socials + phones)."""
    socials = socials or {}
    conn.execute(
        """UPDATE leads SET email = ?, email_status = ?, all_emails = ?,
               email_source = ?, status = ?, facebook = ?, instagram = ?,
               linkedin = ?, youtube = ?, tiktok = ?, twitter = ?,
               website_phones = ?, last_seen = ? WHERE place_key = ?""",
        (email, email_status, ", ".join(all_emails) if all_emails else None,
         source, status, socials.get("facebook"), socials.get("instagram"),
         socials.get("linkedin"), socials.get("youtube"),
         socials.get("tiktok"), socials.get("twitter"),
         ", ".join(phones) if phones else None, now, place_key),
    )
    conn.commit()


def save_llm_emails(conn, place_key, emails_json, now) -> None:
    """Store the validated LLM-written email sequence (JSON) for one lead."""
    conn.execute(
        "UPDATE leads SET llm_emails = ?, last_seen = ? WHERE place_key = ?",
        (emails_json, now, place_key),
    )
    conn.commit()


def leads_missing_emails_copy(conn) -> list:
    """Leads that are sendable (valid email) but have no LLM-written sequence
    yet — the backfill worklist once an API key is available. Returns
    (place_key, name, category, query, rating, reviews, website) dicts."""
    cur = conn.execute(
        """SELECT place_key, name, category, query, rating, reviews, website,
                  reviews_text
           FROM leads
           WHERE email IS NOT NULL AND email != '' AND email_status = 'valid'
             AND (llm_emails IS NULL OR llm_emails = '')
             AND website IS NOT NULL AND website != ''""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ------------------------------------------------------ lead qualifier ----

def leads_for_qualification(conn, query=None, force=False, limit=None) -> list:
    """Full rows (as dicts) ready to score. Runs on whatever data exists — the
    qualifier is rule-based and works with or without enrichment. By default
    only leads not yet qualified (this IS the resume behavior); force=True
    re-scores everything (use after tuning QUALIFY thresholds)."""
    sql = "SELECT * FROM leads"
    where, params = [], []
    if query:
        where.append("query = ?")
        params.append(query)
    if not force:
        where.append("qualify_status IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY reviews DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def save_qualification(conn, place_key, qualification, now) -> None:
    """Record a qualifier verdict for one lead."""
    q = qualification
    conn.execute(
        """UPDATE leads SET quality_score = ?, service_fit = ?, fit_reason = ?,
               qualify_status = ?, last_seen = ? WHERE place_key = ?""",
        (q.quality_score, q.service_fit, q.fit_reason, q.qualify_status,
         now, place_key),
    )
    conn.commit()


def qualified_leads(conn, service_fit=None, query=None) -> list:
    """Leads marked 'qualified' — this is the outreach list. Optionally
    filtered to one service_fit ('web_development' | 'automation' | 'ai')."""
    sql = "SELECT * FROM leads WHERE qualify_status = 'qualified'"
    params = []
    if service_fit:
        sql += " AND service_fit = ?"
        params.append(service_fit)
    if query:
        sql += " AND query = ?"
        params.append(query)
    sql += " ORDER BY quality_score DESC"
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# -------------------------------------------------------------- outreach ----
# Stage 2: free email sequences. Three tables — suppressions (permanent,
# checked before every send), outreach_sequences (per-lead state machine),
# outreach_log (a paper trail of every send attempt).

def add_suppression(conn, email, reason, now) -> None:
    """Permanently opt an address out. Never emailed again, no exceptions."""
    conn.execute(
        "INSERT OR REPLACE INTO suppressions (email, reason, added_at) "
        "VALUES (?, ?, ?)",
        (email.lower(), reason, now),
    )
    conn.commit()


def is_suppressed(conn, email) -> bool:
    if not email:
        return True   # no address at all is treated as "cannot send"
    row = conn.execute(
        "SELECT 1 FROM suppressions WHERE email = ?", (email.lower(),)
    ).fetchone()
    return row is not None


def leads_ready_for_outreach(conn, query=None, vertical=None) -> list:
    """Qualified leads with a verified email, not already suppressed or
    already in a sequence. This is the enqueue candidate list."""
    sql = """
        SELECT l.* FROM leads l
        LEFT JOIN outreach_sequences s ON s.place_key = l.place_key
        LEFT JOIN suppressions sup ON sup.email = LOWER(l.email)
        WHERE l.qualify_status = 'qualified'
          AND l.email IS NOT NULL AND l.email != ''
          AND l.email_status = 'valid'
          AND s.place_key IS NULL
          AND sup.email IS NULL
    """
    params = []
    if query:
        sql += " AND l.query = ?"
        params.append(query)
    if vertical:
        sql += " AND l.vertical = ?"
        params.append(vertical)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def enqueue_sequence(conn, place_key, service_fit, now) -> None:
    """Start a new sequence for a lead, due to send step 1 immediately."""
    conn.execute(
        """INSERT OR IGNORE INTO outreach_sequences
               (place_key, service_fit, current_step, status,
                next_send_at, started_at, updated_at)
           VALUES (?, ?, 0, 'active', ?, ?, ?)""",
        (place_key, service_fit, now, now, now),
    )
    conn.commit()


def lead_row(conn, place_key) -> dict | None:
    """One full lead row as a dict, or None if not found."""
    cur = conn.execute("SELECT * FROM leads WHERE place_key = ?", (place_key,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def sequence_row(conn, place_key) -> dict | None:
    """One sequence + lead info, regardless of status/timing — live.py's
    inline per-lead pipeline needs this right after enqueueing, when the row
    isn't necessarily 'due' yet by due_sequences()'s definition."""
    cur = conn.execute(
        """SELECT s.place_key, s.service_fit, s.current_step, s.next_send_at,
                  s.first_sent_at, l.name, l.email, l.category, l.fit_reason,
                  l.llm_emails
           FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
           WHERE s.place_key = ?""",
        (place_key,),
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def due_sequences(conn, now, vertical=None, latest=False, limit=None,
                  query=None, order=None) -> list:
    """Active sequences whose next step is due, joined with lead contact
    info. This is the send worklist for one outreach.py pass.

    `vertical` restricts to one business type (service_fit); `query`
    restricts to one campaign (the exact scrape query). `order` picks who
    goes first when `limit` caps the batch: 'latest' (newest-enqueued),
    'score' (highest quality first), default oldest-due-first. `latest=True`
    is the legacy spelling of order='latest'."""
    sql = """SELECT s.place_key, s.service_fit, s.current_step, s.next_send_at,
                    s.first_sent_at, s.started_at, l.name, l.email, l.category,
                    l.fit_reason, l.llm_emails
             FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
             WHERE s.status = 'active' AND s.next_send_at <= ?"""
    params = [now]
    if vertical:
        sql += " AND s.service_fit = ?"
        params.append(vertical)
    if query:
        sql += " AND l.query = ?"
        params.append(query)
    order = "latest" if latest else order
    sql += {"latest": " ORDER BY s.started_at DESC",
            "score": " ORDER BY l.quality_score DESC",
            }.get(order, " ORDER BY s.next_send_at")
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def vertical_counts(conn, now) -> list:
    """[{vertical, active, due_now}] for every business type that has at
    least one active sequence — powers the Outreach page's send-by-type
    launcher (the dropdown + the "only N available" validation)."""
    cur = conn.execute(
        """SELECT COALESCE(service_fit, '(untagged)') AS vertical,
                  COUNT(*) AS active,
                  SUM(CASE WHEN next_send_at <= ? THEN 1 ELSE 0 END) AS due_now
           FROM outreach_sequences WHERE status = 'active'
           GROUP BY vertical ORDER BY vertical""",
        (now,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def advance_sequence(conn, place_key, next_step, next_send_at, now,
                     first_sent_at=None) -> None:
    """After a successful send: move to the next step and set when it's due.
    first_sent_at is only written the first time (step 0's actual send time),
    since later steps' delays are anchored to it."""
    conn.execute(
        """UPDATE outreach_sequences SET current_step = ?, next_send_at = ?,
               first_sent_at = COALESCE(first_sent_at, ?),
               last_sent_at = ?, updated_at = ? WHERE place_key = ?""",
        (next_step, next_send_at, first_sent_at, now, now, place_key),
    )
    conn.commit()


def sent_today_count(conn, day: str) -> int:
    """Real sends (not dry-run) logged today — powers the daily send cap."""
    row = conn.execute(
        "SELECT COUNT(*) FROM outreach_log "
        "WHERE status = 'sent' AND dry_run = 0 AND substr(sent_at,1,10) = ?",
        (day,),
    ).fetchone()
    return int(row[0] or 0)


def stop_sequence(conn, place_key, reason, now) -> None:
    conn.execute(
        """UPDATE outreach_sequences SET status = ?, stop_reason = ?,
               updated_at = ? WHERE place_key = ?""",
        ("replied" if reason == "replied" else "stopped", reason, now, place_key),
    )
    conn.commit()


def complete_sequence(conn, place_key, now) -> None:
    conn.execute(
        """UPDATE outreach_sequences SET status = 'completed', updated_at = ?
           WHERE place_key = ?""",
        (now, place_key),
    )
    conn.commit()


def active_sequence_emails(conn) -> list:
    """(place_key, email) for every lead currently in an active sequence —
    the set of addresses to check for replies."""
    return conn.execute(
        """SELECT s.place_key, l.email FROM outreach_sequences s
           JOIN leads l ON l.place_key = s.place_key
           WHERE s.status = 'active' AND l.email IS NOT NULL"""
    ).fetchall()


def log_send(conn, place_key, step, email, subject, status, dry_run, now,
            error=None) -> None:
    conn.execute(
        """INSERT INTO outreach_log
               (place_key, step, email, subject, sent_at, status, dry_run, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (place_key, step, email, subject, now, status, int(dry_run), error),
    )
    conn.commit()


def outreach_stats(conn) -> dict:
    """Aggregate counts for the outreach dashboard — one cheap query set."""
    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total_leads": count("SELECT COUNT(*) FROM leads"),
        "qualified": count("SELECT COUNT(*) FROM leads WHERE qualify_status='qualified'"),
        "low_priority": count("SELECT COUNT(*) FROM leads WHERE qualify_status='low_priority'"),
        "skipped": count("SELECT COUNT(*) FROM leads WHERE qualify_status='skip'"),
        "active_sequences": count("SELECT COUNT(*) FROM outreach_sequences WHERE status='active'"),
        "replied": count("SELECT COUNT(*) FROM outreach_sequences WHERE status='replied'"),
        "completed": count("SELECT COUNT(*) FROM outreach_sequences WHERE status='completed'"),
        "stopped": count("SELECT COUNT(*) FROM outreach_sequences WHERE status='stopped'"),
        "suppressed": count("SELECT COUNT(*) FROM suppressions"),
        "sent_today": sent_today_count(conn, today),
        "daily_cap": SEQUENCE["daily_send_cap"],
        "dry_run": SEQUENCE["dry_run"],
        "service_fit": {
            fit: count(
                "SELECT COUNT(*) FROM leads WHERE service_fit=? AND qualify_status='qualified'",
                (fit,),
            )
            for fit in ("web_development", "automation", "ai")
        },
    }


def all_sequences(conn) -> list:
    """Every sequence regardless of status, joined with lead info — the
    dashboard's leads/campaign table."""
    cur = conn.execute(
        """SELECT s.place_key, s.service_fit, s.current_step, s.status,
                  s.stop_reason, s.next_send_at, s.last_sent_at,
                  l.name, l.email, l.quality_score, l.category, l.query
           FROM outreach_sequences s JOIN leads l ON l.place_key = s.place_key
           ORDER BY s.updated_at DESC"""
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ------------------------------------------------- queue (crash-resume) ----

def clear_queue(conn, query: str) -> None:
    conn.execute("DELETE FROM queue WHERE query = ?", (query,))
    conn.commit()


def enqueue(conn, query: str, listings, now: str) -> None:
    """Add listings as 'pending' work. INSERT OR IGNORE keeps existing status
    (so re-enqueue during a resume never resets a 'done' row)."""
    conn.executemany(
        """INSERT OR IGNORE INTO queue (query, place_key, name, url,
               status, enqueued_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        [(query, l.place_key, l.name, l.url, now) for l in listings],
    )
    conn.commit()


def pending_items(conn, query: str) -> list:
    rows = conn.execute(
        "SELECT name, url, place_key FROM queue WHERE query = ? AND status = 'pending'",
        (query,),
    ).fetchall()
    return [Listing(name=r[0], url=r[1], place_key=r[2]) for r in rows]


def mark_queue(conn, query: str, place_key: str, status: str) -> None:
    conn.execute("UPDATE queue SET status = ? WHERE query = ? AND place_key = ?",
                 (status, query, place_key))
    conn.commit()


# ------------------------------------------------ run_log (rate limit) ----

def record_run(conn, query, started_at, ended_at, extracted, outcome) -> None:
    conn.execute(
        """INSERT INTO run_log (query, started_at, ended_at, extracted, outcome)
           VALUES (?, ?, ?, ?, ?)""",
        (query, started_at, ended_at, extracted, outcome),
    )
    conn.commit()


def extracted_today(conn, day: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(extracted), 0) FROM run_log WHERE substr(started_at,1,10) = ?",
        (day,),
    ).fetchone()
    return int(row[0] or 0)


def last_run_ended(conn) -> str | None:
    row = conn.execute(
        "SELECT MAX(ended_at) FROM run_log WHERE ended_at IS NOT NULL"
    ).fetchone()
    return row[0] if row and row[0] else None


# --------------------------------------------------------------- export ----

class StreamCsv:
    """A CSV that's written row-by-row and flushed to disk immediately, so the
    spreadsheet fills up live during a run and a crash still leaves a valid,
    usable partial file. Missing keys write as blanks; extra keys are ignored."""

    def __init__(self, path, columns):
        self.path = str(path)
        self._f = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._w = csv.DictWriter(self._f, fieldnames=columns,
                                 extrasaction="ignore")
        self._w.writeheader()
        self._f.flush()

    def write(self, row: dict) -> None:
        self._w.writerow(row)
        self._f.flush()   # hit disk now — that's the whole point

    def close(self) -> None:
        self._f.close()


def open_stream(query: str, columns, kind: str = "leads") -> StreamCsv:
    """Open a timestamped streaming CSV for this run."""
    EXPORTS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:50] or "leads"
    path = EXPORTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}-{slug}.csv"
    return StreamCsv(path, columns)


def export_csv(leads, query: str) -> str:
    """Write a full CSV from a list of leads in one shot (used for re-exports)."""
    EXPORTS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:50] or "leads"
    path = EXPORTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(asdict(lead))
    log.debug("wrote %d rows to %s", len(leads), path)
    return str(path)
