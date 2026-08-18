"""
Local web UI for the scraper — a one-click front door.

    python webui/app.py         then open http://127.0.0.1:5050

Serves the page, starts a scrape as a subprocess when you click the button, and
streams the run to the browser over Server-Sent Events. Three event types are
forwarded: 'log' (human log lines), 'lead' (a freshly scraped business),
'leadupdate' (enrichment result for a lead), and 'done'. Runs entirely on your
machine; nothing is exposed to the network by default.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

BASE = Path(__file__).resolve().parent.parent   # project root
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from config import (IDENTITY_IS_PLACEHOLDER, SEQUENCE,  # noqa: E402
                    TEST_SEND_ADDRESS)
from sequencer import mailer                  # noqa: E402
from storage import campaigns, store          # noqa: E402
from webui.outreach_api import bp as outreach_api_bp  # noqa: E402

app = Flask(__name__)
app.register_blueprint(outreach_api_bp)

_state = {"proc": None, "events": [], "done": True, "kind": None,
          "label": None, "lock": threading.Lock()}
_MARK_LEAD = "@@LEAD@@"
_MARK_UPDATE = "@@LEADUPDATE@@"
_MARK_CURSOR = "@@CURSOR@@"
_MARK_SYNC = "@@SYNC@@"


def _reader(proc):
    """Pump subprocess output into the shared buffer, classifying each line."""
    for raw in iter(proc.stdout.readline, ""):
        line = raw.rstrip("\n")
        if line.startswith(_MARK_LEAD):
            event = ("lead", line[len(_MARK_LEAD):])
        elif line.startswith(_MARK_UPDATE):
            event = ("leadupdate", line[len(_MARK_UPDATE):])
        elif line.startswith(_MARK_CURSOR):
            event = ("cursor", line[len(_MARK_CURSOR):])
        elif line.startswith(_MARK_SYNC):
            event = ("sync", line[len(_MARK_SYNC):])
        else:
            event = ("log", line)
        with _state["lock"]:
            _state["events"].append(event)
    proc.stdout.close()
    proc.wait()
    with _state["lock"]:
        _state["done"] = True
        _state["events"].append(("done", ""))


@app.route("/")
def index():
    return send_file(HERE / "index.html")


# The Outreach dashboard is a React SPA (frontend/, built with Vite into
# webui/dist with base=/outreach/). All four page routes serve the same
# index.html — React Router picks the page client-side. Until a build
# exists, fall back to the legacy board so /outreach never 404s.
_DIST = HERE / "dist"


@app.route("/outreach")
@app.route("/outreach/campaigns")
@app.route("/outreach/sender")
@app.route("/outreach/sequences")
def outreach_spa():
    index = _DIST / "index.html"
    if index.exists():
        return send_file(index)
    return send_file(HERE / "outreach-legacy.html")


@app.route("/outreach/assets/<path:filename>")
def outreach_assets(filename):
    return send_file(_DIST / "assets" / filename)


@app.route("/outreach/legacy")
def outreach_legacy_page():
    return send_file(HERE / "outreach-legacy.html")   # the old board UI


@app.route("/terminal")
def terminal_page():
    return send_file(HERE / "terminal.html")


def _run_subprocess(cmd, label, kind="scrape"):
    """Shared launcher for any of the CLI tools (scrape/live/qualify/
    outreach) — one run at a time, streamed to the browser via /stream.
    `kind` lets a reloaded page tell whether an in-progress run is one it
    should reconnect to (see /api/status)."""
    with _state["lock"]:
        if _state["proc"] and _state["proc"].poll() is None:
            return jsonify({"error": "Something is already running."}), 409
        _state["events"] = []
        _state["done"] = False
        _state["kind"] = kind
        _state["label"] = label
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
           "LEADSCAN_EMIT_JSON": "1"}
    proc = subprocess.Popen(
        cmd, cwd=str(BASE), env=env, text=True, encoding="utf-8", bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    _state["proc"] = proc
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return jsonify({"ok": True, "command": label})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True, silent=True) or {}
    niche = (data.get("niche") or "").strip()
    city = (data.get("city") or "").strip()
    url = (data.get("url") or "").strip()
    if not url and (not niche or not city):
        return jsonify({"error": "Enter a business type and location, "
                                 "or paste a Google Maps URL."}), 400

    vertical = (data.get("serviceFit") or "").strip()
    if url:
        cmd = [sys.executable, "-u", "main.py", "--url", url]
    elif vertical:
        # A vertical means "search everything in its profile". One phrase against
        # one city returns whatever Google has for it — 36 for private dentists
        # in Manchester — so asking for 500 and getting 36 isn't a cap, it's a
        # single search. batch.py walks the terms x cities matrix and stops at
        # the target.
        cmd = [sys.executable, "-u", "batch.py", "--vertical", vertical]
    else:
        # main.py takes the query as ONE positional string — nothing splits on
        # commas anywhere. (batch.py comma-splits --niches/--cities AND --terms,
        # so "Dallas, TX" kept becoming two searches, one of them garbage.)
        cmd = [sys.executable, "-u", "main.py", f"{niche} in {city}"]
    if data.get("limit"):
        cmd += ["--limit", str(int(data["limit"]))]
    if data.get("minRating"):
        cmd += ["--min-rating", str(float(data["minRating"]))]
    if data.get("withWebsite"):
        cmd += ["--with-website"]
    if data.get("skipClosed"):
        cmd += ["--skip-closed"]
    fields = data.get("fields") or []
    if fields:
        cmd += ["--fields", ",".join(fields)]   # also decides enrichment
    if vertical and "--vertical" not in cmd:
        cmd += ["--vertical", vertical]      # tags leads for the gate + routing

    return _run_subprocess(cmd, " ".join(cmd[2:]), kind="scrape")


@app.route("/api/verticals")
def verticals_list():
    """The vertical profiles, for the scrape page's dropdown. Picking one fills
    in its search terms and cities so the operator doesn't retype what's
    already written down in profiles/verticals.json."""
    from qualifier import profiles
    out = []
    for name in profiles.names():
        profile = profiles.load(name)
        if profile:
            out.append({"name": name, "slug": profile["slug"],
                        "tier": profile["tier"],
                        "terms": profile.get("search_terms") or [],
                        "cities": profile.get("cities") or []})
    return jsonify(out)


@app.route("/api/gate/start", methods=["POST"])
def gate_start():
    """Run the gate over already-scraped leads of one vertical. Reads the
    database and writes the spreadsheet; it can never send an email."""
    data = request.get_json(force=True, silent=True) or {}
    vertical = (data.get("vertical") or "").strip()
    if not vertical:
        return jsonify({"error": "Pick a business vertical first — the gate "
                                 "needs to know which profile to judge against."}), 400
    # Refuse an unknown profile here rather than launching a subprocess that
    # fails somewhere in a log. A refusal the operator can't see reads as
    # "nothing happened".
    from qualifier import profiles
    if profiles.load(vertical) is None:
        return jsonify({"error": f"No profile called '{vertical}'. Add it to "
                                 f"profiles/verticals.json first."}), 400
    cmd = [sys.executable, "-u", "rungate.py", "--vertical", vertical]
    if data.get("noSheets"):
        cmd += ["--no-sheets"]
    return _run_subprocess(cmd, f"rungate.py --vertical {vertical}", kind="gate")


@app.route("/api/qualify/start", methods=["POST"])
def qualify_start():
    data = request.get_json(force=True, silent=True) or {}
    cmd = [sys.executable, "-u", "qualify.py"]
    if data.get("force"):
        cmd += ["--force"]
    return _run_subprocess(cmd, "qualify.py", kind="qualify")


@app.route("/api/outreach/start", methods=["POST"])
def outreach_start():
    # UI-triggered runs are ALWAYS dry-run — --live is intentionally never
    # wired up here. A real campaign send stays a deliberate CLI action
    # (see outreach.py's two-key gate), never a single button click.
    cmd = [sys.executable, "-u", "outreach.py"]
    return _run_subprocess(cmd, "outreach.py (dry run — nothing real sends)",
                           kind="outreach")


@app.route("/api/outreach/stats")
def outreach_stats():
    conn = store.connect()
    try:
        stats = store.outreach_stats(conn)
    finally:
        conn.close()
    stats["test_send_address"] = TEST_SEND_ADDRESS
    return jsonify(stats)


@app.route("/api/outreach/sequences")
def outreach_sequences():
    conn = store.connect()
    try:
        seqs = store.all_sequences(conn)
    finally:
        conn.close()
    return jsonify(seqs)


@app.route("/api/outreach/vertical-counts")
def outreach_vertical_counts():
    """Business types with an active sequence, and how many leads are
    available right now — powers the send-by-business-type launcher's
    dropdown and its "only N available" validation."""
    conn = store.connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = store.vertical_counts(conn, now)
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/outreach/send-vertical", methods=["POST"])
def outreach_send_vertical():
    """The Outreach page's deliberate send-by-business-type action: sends to
    up to `count` due leads of one business type (optionally the newest
    `count` instead of the oldest-due-first default). Always intends a REAL
    send — the page's confirm dialog IS the human checkpoint — but the same
    two-key gate still applies: config.py's SEQUENCE['dry_run'] must be
    False too, or this refuses loudly instead of silently doing nothing."""
    data = request.get_json(force=True, silent=True) or {}
    vertical = (data.get("vertical") or "").strip()
    if not vertical:
        return jsonify({"error": "Pick a business type."}), 400
    count = data.get("count")
    if not isinstance(count, int) or count < 1:
        return jsonify({"error": "Enter how many leads to send to."}), 400

    conn = store.connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = store.vertical_counts(conn, now)
    finally:
        conn.close()
    available = next((r["due_now"] for r in rows if r["vertical"] == vertical), 0)
    if count > available:
        return jsonify({"error": f"Only {available} lead(s) available for "
                        f"'{vertical}' right now — lower the count."}), 400

    if SEQUENCE["dry_run"]:
        return jsonify({"error": "Real sending is off: config.py's "
                        "SEQUENCE['dry_run'] is still True. Set it to False "
                        "in config.py to actually send."}), 400
    if IDENTITY_IS_PLACEHOLDER:
        return jsonify({"error": "Set your sender identity first: "
                        "OUTREACH_FROM_NAME, OUTREACH_BUSINESS_NAME and "
                        "OUTREACH_BOOKING_URL in .env (see .env.example). "
                        "Emails must carry YOUR name and booking link."}), 400

    cmd = [sys.executable, "-u", "outreach.py", "--vertical", vertical,
           "--count", str(count), "--live"]
    if data.get("latest"):
        cmd += ["--latest"]
    label = f"outreach.py --vertical {vertical} --count {count} --live"
    return _run_subprocess(cmd, label, kind="outreach_vertical")


@app.route("/api/outreach/send-campaign", methods=["POST"])
def outreach_send_campaign():
    """The Email Sender page's deliberate send action: up to `count` due
    leads of ONE campaign (scrape query), ordered latest/oldest/score.
    Always intends a REAL send — the page's confirm dialog is the human
    checkpoint — but config.py's SEQUENCE['dry_run'] must be False too."""
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Pick a campaign."}), 400
    count = data.get("count")
    if not isinstance(count, int) or count < 1:
        return jsonify({"error": "Enter how many emails to send."}), 400
    order = data.get("order") or "oldest"
    if order not in ("latest", "oldest", "score"):
        return jsonify({"error": "Bad send order."}), 400

    conn = store.connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = campaigns.campaigns(conn, now)
    finally:
        conn.close()
    available = next((r["due_now"] for r in rows if r["query"] == query), 0)
    if count > available:
        return jsonify({"error": f"Only {available} lead(s) ready in this "
                        f"campaign right now — lower the count."}), 400

    if SEQUENCE["dry_run"]:
        return jsonify({"error": "Real sending is off: config.py's "
                        "SEQUENCE['dry_run'] is still True. Set it to False "
                        "in config.py to actually send."}), 400
    if IDENTITY_IS_PLACEHOLDER:
        return jsonify({"error": "Set your sender identity first: "
                        "OUTREACH_FROM_NAME, OUTREACH_BUSINESS_NAME and "
                        "OUTREACH_BOOKING_URL in .env (see .env.example). "
                        "Emails must carry YOUR name and booking link."}), 400

    cmd = [sys.executable, "-u", "outreach.py", "--campaign", query,
           "--count", str(count), "--order", order, "--live",
           "--skip-replies"]
    label = f"send {count} to campaign '{query}' ({order} first)"
    return _run_subprocess(cmd, label, kind="outreach_campaign")


@app.route("/api/outreach/test-send", methods=["POST"])
def outreach_test_send():
    """Sends exactly ONE real email to a hardcoded safe address (config.py's
    TEST_SEND_ADDRESS) — never influenced by request data, so it can never
    reach a real lead. The only endpoint that bypasses dry-run, by design."""
    subject = "Outreach engine — test email"
    body = ("This is a real test send from your outreach engine's web "
            "dashboard, sent only to this fixed test address to verify "
            "formatting and deliverability.\n\nNo real leads were contacted.")
    result = mailer.send_email(TEST_SEND_ADDRESS, subject, body, dry_run=False)
    if result.status == "sent":
        return jsonify({"ok": True, "message": f"Sent to {TEST_SEND_ADDRESS}"})
    return jsonify({"ok": False, "error": result.error or "send failed"}), 500


@app.route("/api/live/start", methods=["POST"])
def live_start():
    """Scrape page's 'Sync with outreach' button (also used by the Terminal
    page): scrape -> enrich -> qualify -> send, per lead, live. --live is
    only added when the caller explicitly opts in (the 'send for real'
    checkbox) — even then, config.py's SEQUENCE['dry_run'] must ALSO be
    False, so a real send always needs a deliberate edit to this repo, not
    just a checkbox click."""
    data = request.get_json(force=True, silent=True) or {}
    niche = (data.get("niche") or "").strip()
    city = (data.get("city") or "").strip()
    if not niche or not city:
        return jsonify({"error": "Need a business type and a location."}), 400

    # Fail loud and BEFORE launching anything if "send for real" is checked
    # but the code-level gate is still closed — otherwise the run silently
    # refuses deep inside the subprocess (a single easily-missed log line)
    # and nothing scrapes OR sends, which looks like nothing happened at all.
    if data.get("live") and SEQUENCE["dry_run"]:
        return jsonify({"error": "Real sending is off: config.py's "
                        "SEQUENCE['dry_run'] is still True. Set it to False "
                        "in config.py to actually send, or uncheck 'send for "
                        "real' to keep previewing."}), 400
    if data.get("live") and IDENTITY_IS_PLACEHOLDER:
        return jsonify({"error": "Set your sender identity first: "
                        "OUTREACH_FROM_NAME, OUTREACH_BUSINESS_NAME and "
                        "OUTREACH_BOOKING_URL in .env (see .env.example)."}), 400

    query = f"{niche} in {city}"
    cmd = [sys.executable, "-u", "live.py", query]
    if data.get("limit"):
        cmd += ["--limit", str(int(data["limit"]))]
    if data.get("minRating"):
        cmd += ["--min-rating", str(float(data["minRating"]))]
    if data.get("withWebsite"):
        cmd += ["--with-website"]
    if data.get("skipClosed"):
        cmd += ["--skip-closed"]
    fields = data.get("fields") or []
    if fields:
        cmd += ["--fields", ",".join(fields)]
    if data.get("syncQualify"):
        cmd += ["--sync-qualify"]
    if data.get("syncSend"):
        cmd += ["--sync-send"]
    if data.get("live"):
        cmd += ["--live"]
    if data.get("serviceFit"):
        cmd += ["--vertical", str(data["serviceFit"])]   # email routing key

    return _run_subprocess(cmd, " ".join(cmd[2:]), kind="live")


@app.route("/api/status")
def run_status():
    """Is a run in progress, and what kind? Lets a reloaded page reconnect to
    a run it started earlier (e.g. you navigated away mid-scrape and back)."""
    with _state["lock"]:
        running = not _state["done"]
        return jsonify({
            "running": running,
            "kind": _state["kind"] if running else None,
            "command": _state["label"] if running else None,
        })


@app.route("/stream")
def stream():
    """SSE: replay buffered events, then follow live. Emits typed events:
    log / lead / leadupdate / done."""
    def gen():
        idx = 0
        while True:
            with _state["lock"]:
                new = _state["events"][idx:]
                idx = len(_state["events"])
                finished = _state["done"] and not new
            for etype, payload in new:
                yield f"event: {etype}\ndata: {payload}\n\n"
                if etype == "done":
                    return
            if finished:
                yield "event: done\ndata: \n\n"
                return
            time.sleep(0.25)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    print(f"\n  Lead Scanner UI  ->  http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True)
