"""
Relative dates to absolute ones.

Google Maps never prints a review's real date — it prints "2 months ago". That
is fine for a human reading one review and useless for us: the gate has to drop
evidence older than a year, and the email wants to say "three reviews since
June". Both need a real date, so we convert at capture time, while "ago" still
means what it said.

Approximate on purpose. A month is 30 days here; nobody is auditing whether a
complaint landed on the 28th or the 31st.
"""

import re
from datetime import date, timedelta

from core.logbook import get_logger

log = get_logger(__name__)

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# "3 months ago", "a week ago", "an hour ago". A leading article means one.
_RELATIVE_RE = re.compile(
    r"\b(?:(\d+)|an?)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE)


def parse_relative(text: str, today: date | None = None) -> date | None:
    """'3 months ago' -> a date. None if the string isn't a relative date."""
    if not text:
        return None
    today = today or date.today()
    lowered = text.strip().lower()
    if lowered in ("today", "just now", "a moment ago"):
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)

    found = _RELATIVE_RE.search(lowered)
    if not found:
        return None
    count = int(found.group(1)) if found.group(1) else 1
    unit = found.group(2)
    # anything under a day rounds to today — the precision is meaningless here
    return today - timedelta(days=count * _UNIT_DAYS.get(unit, 0))


def months_since(when: date, today: date | None = None) -> int:
    """Whole months between a date and today. Negative dates clamp to 0."""
    today = today or date.today()
    return max(0, (today - when).days // 30)


def is_recent(when: date | None, max_months: int, today: date | None = None) -> bool:
    """Is this evidence fresh enough to quote? Unknown dates are not."""
    if when is None:
        return False
    return months_since(when, today) <= max_months
