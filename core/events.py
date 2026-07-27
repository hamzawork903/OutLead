"""
Structured events for the web UI. When the scraper runs under the UI, the Flask
server sets LEADSCAN_EMIT_JSON=1; each saved lead is then also printed as a
machine-readable line (`@@LEAD@@{json}` / `@@LEADUPDATE@@{json}`) which the
server forwards to the browser so the live results table can fill in real time.

On the normal CLI (env unset) these emit nothing, so console output stays clean.
"""

import json
import os
import sys

_ENABLED = os.environ.get("LEADSCAN_EMIT_JSON") == "1"

LEAD = "LEAD"              # a freshly extracted lead (Maps fields)
LEAD_UPDATE = "LEADUPDATE"  # enrichment result for an existing lead
CURSOR = "CURSOR"          # which listing is being worked on right now
SYNC = "SYNC"              # live.py per-lead pipeline stage (found/enriched/
                          # qualified/sent) — narrated live in the terminal


def emit(kind: str, payload: dict) -> None:
    if not _ENABLED:
        return
    try:
        sys.stdout.write(f"@@{kind}@@" + json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass   # telemetry must never break a run
