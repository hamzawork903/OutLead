"""
The one place that talks to the LLM, and the one place that counts what it
costs.

Two callers need this: the email writer, and the gate's judge. They ask
different questions but they share a wallet, so the $5 cap has to be enforced
here rather than in each of them — two separate budgets would both think they
were within their limit.

Spend is estimated from the API's own usage counts and persisted, so it
survives restarts and accumulates across scrape, enrich and gate runs.

Needs OPENAI_API_KEY in .env (loaded by config.py's dotenv call).
"""

import json
import os
import threading

import httpx

from config import LLM, LOGS_DIR
from core.logbook import get_logger

log = get_logger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"

_SPEND_PATH = LOGS_DIR / "llm_spend.json"
_spend_lock = threading.Lock()
_warned_no_key = False


def spent_usd() -> float:
    """Total estimated spend so far, across every run."""
    try:
        return float(json.loads(_SPEND_PATH.read_text())["spent_usd"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 0.0


def record_spend(prompt_tokens: int, completion_tokens: int) -> float:
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


def available() -> bool:
    """Is the LLM usable at all — enabled, keyed, and inside budget?"""
    global _warned_no_key
    key = os.environ.get("OPENAI_API_KEY")
    if not LLM["enabled"]:
        return False
    if not key:
        if not _warned_no_key:
            _warned_no_key = True
            log.info("  LLM off: OPENAI_API_KEY not set in .env")
        return False
    spent = spent_usd()
    if spent >= LLM["budget_usd"]:
        log.error("LLM NOT WORKING: the $%.2f budget is used up ($%.4f spent). "
                  "Nothing more will be generated until LLM['budget_usd'] is "
                  "raised in config.py.", LLM["budget_usd"], spent)
        return False
    return True


def ask_json(system: str, user: str, max_tokens: int, label: str = "",
             temperature: float = 0.2) -> str | None:
    """One JSON-mode completion. Returns the raw string for the caller to
    validate, or None on any failure — never raises.

    Deliberately returns raw text rather than a parsed object: the model's
    output is untrusted input, and every caller has its own schema to enforce
    before anything downstream is allowed to believe it.

    `temperature` matters more than it looks. The email writer wants some
    variety; a classifier must not have any. At 0.2 the gate's judge returned
    healthy_business=True and then False for the same business on consecutive
    runs, so a Tier A lead appeared and vanished between two identical
    gate runs."""
    if not available():
        return None
    body = {
        "model": LLM["model"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    try:
        resp = httpx.post(API_URL, json=body, timeout=LLM["timeout_s"],
                          headers={"Authorization":
                                   f"Bearer {os.environ['OPENAI_API_KEY']}"})
        if resp.status_code != 200:
            log.warning("LLM call failed (HTTP %d)%s", resp.status_code,
                        f" for {label}" if label else "")
            return None
        payload = resp.json()
        raw = payload["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as err:
        log.warning("LLM call failed (%s)%s", type(err).__name__,
                    f" for {label}" if label else "")
        return None

    usage = payload.get("usage") or {}
    record_spend(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return raw
