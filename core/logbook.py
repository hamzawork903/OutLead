"""
The logbook — one place every module records what it's doing.

Why this exists: when something breaks weeks from now, we want a timestamped,
moment-by-moment record with the EXACT error, not hours of guessing. Every
feature, small or big, logs through here.

Two outputs, on purpose (the two-perspectives rule):
  - CONSOLE  → clean, plain-language, for you watching a run. Errors/warnings
               get a small tag; tracebacks are NOT dumped here.
  - LOG FILE → logs/<timestamp>-<label>.log. Full detail: timestamps, module
               names, levels, every debug breadcrumb, and complete tracebacks.

Usage in any module:
    from core.logbook import get_logger
    log = get_logger(__name__)

    log.info("Maps is ready (%.1fs).", elapsed)   # -> console + file
    log.debug("feed scrollHeight=%d", height)      # -> file only
    log.warning("feed stalled at %d", count)       # -> console [warn] + file
    log.exception("warm-up failed")                # -> console [error],
                                                   #    file gets full traceback

Call setup_logging(...) ONCE at program start (main.py does this).
"""

import logging
import re
import sys
from datetime import datetime

from config import LOGS_DIR

_FILE_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE_FMT = "%H:%M:%S"

# Module-facing extras so a module can flag a saved artifact (e.g. screenshot)
# that belongs with a log line. Purely for the file; console ignores it.


class _ConsoleFormatter(logging.Formatter):
    """Keep the console clean: plain message, small tag for warn/error,
    and never print a traceback here (that's what the file is for)."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return f"  [error] {msg}"
        if record.levelno >= logging.WARNING:
            return f"  [warn]  {msg}"
        return msg


def setup_logging(label: str = "run") -> str:
    """
    Configure console + file logging for this run. Returns the log file path
    so the caller can tell the operator where the detailed record lives.
    Safe to call more than once (handlers are reset, not duplicated).
    """
    LOGS_DIR.mkdir(exist_ok=True)

    # Make the console UTF-8 so dashes/quotes render instead of mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except Exception:
            pass

    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:50] or "run"
    path = LOGS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):   # avoid duplicate lines on re-setup
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)        # debug stays out of the operator's view
    console.setFormatter(_ConsoleFormatter())

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # everything goes to the file
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))

    root.addHandler(console)
    root.addHandler(file_handler)

    # Quiet chatty third-party loggers — their per-request noise belongs
    # nowhere near the operator's clean console.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("logbook").debug(
        "=== run start: %s | %s ===", label, datetime.now().isoformat(timespec="seconds")
    )
    return str(path)


def get_logger(name: str) -> logging.Logger:
    """Get a module logger. Handlers live on the root (configured once)."""
    return logging.getLogger(name)
