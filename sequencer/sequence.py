"""
The sequence state machine — pure logic, no DB, no network, fully testable.

A sequence moves through steps 0, 1, 2, ... (one per entry in
config.SEQUENCE["step_delay_days"]). Step 0 sends immediately; each later step
waits its configured number of days after the FIRST send. The machine only
answers two questions:

  1. Given a sequence's current step, what step comes next (or is it done)?
  2. When should the sequence be checked again?

Everything about WHETHER to send right now (suppression, replies, rate caps)
is a separate concern, checked by outreach.py before it acts on this.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import SEQUENCE

# Timing override, injected at startup by callers that own a DB connection
# (outreach.py / live.py call set_delays(campaigns.step_delays(conn))). This
# module stays pure — no DB access — while the user-edited timing from the
# Sequences page still wins over the config default.
_delays_override: list | None = None


def set_delays(delays: list | None) -> None:
    global _delays_override
    _delays_override = delays


def _delays() -> list:
    return _delays_override or SEQUENCE["step_delay_days"]


@dataclass
class StepPlan:
    step: int                 # the step number about to be sent (0-based)
    is_final: bool             # True if there is no step after this one
    next_send_at: str | None   # ISO timestamp for the step AFTER this one


def total_steps() -> int:
    return len(_delays())


def plan_step(current_step: int, first_sent_at: str | None, now: str) -> StepPlan | None:
    """What to send next for a sequence currently at `current_step`.

    Returns None if the sequence has already completed all steps.
    `first_sent_at` is the ISO timestamp of step 0's send (None if step 0
    hasn't gone out yet); later steps are scheduled relative to it so a late
    step-0 send doesn't compound delays down the line.
    """
    delays = _delays()
    if current_step >= len(delays):
        return None

    is_final = current_step == len(delays) - 1
    if is_final:
        return StepPlan(step=current_step, is_final=True, next_send_at=None)

    anchor = _parse(first_sent_at) if first_sent_at else _parse(now)
    next_due = anchor + timedelta(days=delays[current_step + 1])
    return StepPlan(step=current_step, is_final=False,
                    next_send_at=next_due.isoformat(timespec="seconds"))


def checklist(lead_email: str | None, suppressed: bool):
    """Final pre-send gate, re-checked fresh at every send (not just at
    enqueue time) — a lead can get suppressed between steps. Returns
    (ok: bool, reason: str|None)."""
    if not lead_email:
        return False, "no email address"
    if suppressed:
        return False, "suppressed (opted out)"
    return True, None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)
