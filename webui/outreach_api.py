"""
Read + small-action APIs for the new Outreach dashboard (Blueprint, mounted
by app.py). Anything that LAUNCHES a subprocess (scrapes, send passes) stays
in app.py next to _run_subprocess; everything here is a direct DB read or a
single-row action.
"""

import json
import os
import threading
import time
from datetime import datetime

import httpx
from flask import Blueprint, jsonify, request

from config import LLM, LOGS_DIR, SEQUENCE
from core.logbook import get_logger
from storage import campaigns, store

log = get_logger(__name__)
bp = Blueprint("outreach_api", __name__)

_MAX_STEPS = 3   # the LLM writes exactly 3 emails per lead — timing is
                 # editable, the step count can't exceed the copy we have


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@bp.route("/api/outreach/campaigns")
def api_campaigns():
    conn = store.connect()
    try:
        return jsonify(campaigns.campaigns(conn, _now()))
    finally:
        conn.close()


@bp.route("/api/outreach/campaign-leads")
def api_campaign_leads():
    query = request.args.get("query", "")
    if not query:
        return jsonify({"error": "query parameter required"}), 400
    conn = store.connect()
    try:
        return jsonify(campaigns.campaign_leads(conn, query))
    finally:
        conn.close()


@bp.route("/api/lead/<place_key>")
def api_lead_detail(place_key):
    conn = store.connect()
    try:
        lead = campaigns.lead_detail(conn, place_key)
    finally:
        conn.close()
    if lead is None:
        return jsonify({"error": "lead not found"}), 404
    return jsonify(lead)


@bp.route("/api/lead/<place_key>/stop", methods=["POST"])
def api_lead_stop(place_key):
    conn = store.connect()
    try:
        store.stop_sequence(conn, place_key, "stopped_manually", _now())
    finally:
        conn.close()
    log.info("sequence stopped manually: %s", place_key)
    return jsonify({"ok": True})


@bp.route("/api/lead/<place_key>/suppress", methods=["POST"])
def api_lead_suppress(place_key):
    conn = store.connect()
    try:
        lead = store.lead_row(conn, place_key)
        if not lead or not lead.get("email"):
            return jsonify({"error": "lead has no email"}), 400
        store.add_suppression(conn, lead["email"], "manual", _now())
        store.stop_sequence(conn, place_key, "suppressed", _now())
    finally:
        conn.close()
    log.info("suppressed manually: %s", place_key)
    return jsonify({"ok": True})


@bp.route("/api/outreach/attention")
def api_attention():
    conn = store.connect()
    try:
        return jsonify(campaigns.attention(conn))
    finally:
        conn.close()


@bp.route("/api/sequence-config", methods=["GET", "POST"])
def api_sequence_config():
    conn = store.connect()
    try:
        if request.method == "GET":
            return jsonify({"delays": campaigns.step_delays(conn),
                            "max_steps": _MAX_STEPS})
        data = request.get_json(force=True, silent=True) or {}
        delays = data.get("delays")
        if (not isinstance(delays, list) or not (1 <= len(delays) <= _MAX_STEPS)
                or delays[0] != 0
                or not all(isinstance(d, int) and 0 <= d <= 60 for d in delays)
                or delays != sorted(delays)):
            return jsonify({"error": "Timing must be 1-3 steps: the first is "
                            "day 0, later days increase, max 60 days."}), 400
        campaigns.save_step_delays(conn, delays)
        return jsonify({"ok": True, "delays": delays})
    finally:
        conn.close()


# ------------------------------------------------------------- health ----
# Reoon balance is an external call — cache it briefly so dashboard loads
# don't hammer (or hang on) their API.
_health_cache = {"at": 0.0, "reoon": None, "refreshing": False}
_health_lock = threading.Lock()


def _fetch_reoon_balance():
    key = os.environ.get("REOON_API_KEY")
    balance = None
    if key:
        try:
            r = httpx.get("https://emailverifier.reoon.com/api/v1/"
                          "check-account-balance/", params={"key": key},
                          timeout=8)
            if r.status_code == 200:
                d = r.json()
                balance = (d.get("remaining_daily_credits", 0)
                           + d.get("remaining_instant_credits", 0))
        except (httpx.HTTPError, ValueError):
            balance = None
    with _health_lock:
        _health_cache.update(at=time.time(), reoon=balance, refreshing=False)


def _reoon_balance():
    """Never blocks the dashboard on a third-party API. Returns whatever we
    last knew (None on a cold start) and refreshes in the background when the
    value goes stale — otherwise every 5 minutes one unlucky page load waits
    seconds for Reoon before the health chips can render."""
    with _health_lock:
        stale = time.time() - _health_cache["at"] > 300
        if stale and not _health_cache["refreshing"]:
            _health_cache["refreshing"] = True
            threading.Thread(target=_fetch_reoon_balance, daemon=True).start()
        return _health_cache["reoon"]


@bp.route("/api/outreach/health")
def api_health():
    spent = 0.0
    try:
        spent = float(json.loads((LOGS_DIR / "llm_spend.json").read_text())
                      .get("spent_usd", 0.0))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return jsonify({
        "llm_spent_usd": round(spent, 4),
        "llm_budget_usd": LLM["budget_usd"],
        "llm_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        "reoon_credits": _reoon_balance(),
        "dry_run": SEQUENCE["dry_run"],
    })
