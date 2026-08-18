"""
Google Sheets export for gated leads. One tab per vertical, plus a shared tab
of everything the gate rejected and why.

The sheet is a MIRROR, not a control surface. This module writes; nothing it
writes can cause an email to be sent. The one thing read back is a
`do_not_contact` column, and that can only ever prevent a send — never trigger
one. Anything that could start an outreach stays behind the two-key gate.

Fails soft on purpose: a network blip or an expired key must never take down a
scrape or a gate run. Every function logs and returns rather than raising, and
the CSV export in exports/ remains the source you can always fall back on.
"""

import gspread
from google.oauth2.service_account import Credentials

from config import SHEET_COLUMNS, SHEETS, SHEETS_ENABLED
from core.logbook import get_logger

log = get_logger(__name__)

# Sheets *and* Drive: gspread needs Drive to look a spreadsheet up by key.
_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",
           "https://www.googleapis.com/auth/drive.file")


def _open_spreadsheet():
    """The configured spreadsheet, or None (logged) if it can't be reached."""
    if not SHEETS_ENABLED:
        log.debug("sheets export off — no key file or spreadsheet id set")
        return None
    try:
        creds = Credentials.from_service_account_file(
            SHEETS["key_file"], scopes=list(_SCOPES))
        client = gspread.authorize(creds)
        return client.open_by_key(SHEETS["spreadsheet_id"])
    except FileNotFoundError:
        log.error("sheets: key file not found at %s", SHEETS["key_file"])
    except gspread.exceptions.APIError as err:
        log.error("sheets: API refused the request (%s) — is the sheet shared "
                  "with the service account as Editor?", err)
    except Exception as err:                    # auth, DNS, clock skew, ...
        log.error("sheets: could not open spreadsheet (%s: %s)",
                  type(err).__name__, err)
    return None


def tab_name(vertical: str) -> str:
    """A vertical name Google will accept as a tab title.

    Sheets rejects : \\ / ? * [ ] outright and caps titles at 100 characters,
    and "Boiler & Gas / Heating Engineer" is rank 2 on the list — so this isn't
    hypothetical."""
    cleaned = "".join(" " if c in r':\/?*[]' else c for c in (vertical or ""))
    return " ".join(cleaned.split())[:95] or "Unnamed"


def _ensure_tab(spreadsheet, title: str):
    """The named worksheet, with a header row matching SHEET_COLUMNS.

    Re-syncs the header when the column list changes. Without this, adding a
    column silently shifts every cell one place right — the sheet would still
    look populated while quietly showing the wrong data under each heading."""
    try:
        tab = spreadsheet.worksheet(title)
        if tab.row_values(1) != SHEET_COLUMNS:
            # Existing rows were written against the old header, so once the
            # columns change they no longer line up with it. Leaving them shifts
            # every cell one place; keeping their keys makes the upsert miss and
            # append a second copy of everything. Clear and let the database —
            # which is the source of truth — write them back.
            log.warning("sheets: columns changed on %r — clearing and "
                        "rewriting its rows from the database", title)
            tab.clear()
            tab.update([SHEET_COLUMNS], "A1")
            tab.freeze(rows=1)
        return tab
    except gspread.exceptions.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(title=title, rows=1000,
                                        cols=len(SHEET_COLUMNS))
        tab.update([SHEET_COLUMNS], "A1")
        tab.freeze(rows=1)
        log.info("sheets: created tab %r", title)
        return tab


def _row_from(lead: dict) -> list:
    """One lead as a row in SHEET_COLUMNS order. Missing keys become blank."""
    return ["" if lead.get(col) is None else str(lead.get(col))
            for col in SHEET_COLUMNS]


def _existing_keys(tab, key_column: str) -> dict:
    """{dedupe key: row number} for rows already in the tab."""
    if key_column not in SHEET_COLUMNS:
        return {}
    try:
        values = tab.col_values(SHEET_COLUMNS.index(key_column) + 1)
    except Exception as err:
        log.warning("sheets: couldn't read existing keys (%s)", type(err).__name__)
        return {}
    return {value: row for row, value in enumerate(values[1:], start=2) if value}


def write_leads(tab_title: str, leads: list) -> int:
    """Append leads to a tab, updating any row whose dedupe key already exists.
    Returns how many rows were written. 0 on any failure — never raises."""
    if not leads:
        return 0
    spreadsheet = _open_spreadsheet()
    if spreadsheet is None:
        return 0
    tab_title = tab_name(tab_title)
    try:
        tab = _ensure_tab(spreadsheet, tab_title)
        seen = _existing_keys(tab, SHEETS["dedupe_key"])
        fresh, updates = [], []
        for lead in leads:
            row = _row_from(lead)
            at = seen.get(str(lead.get(SHEETS["dedupe_key"], "")))
            if at:
                updates.append({"range": f"A{at}", "values": [row]})
            else:
                fresh.append(row)

        if updates:
            tab.batch_update(updates)
        for start in range(0, len(fresh), SHEETS["batch_size"]):
            tab.append_rows(fresh[start:start + SHEETS["batch_size"]],
                            value_input_option="RAW")
        log.info("sheets: %r +%d new, %d updated", tab_title, len(fresh),
                 len(updates))
        return len(fresh) + len(updates)
    except Exception as err:
        log.error("sheets: write to %r failed (%s: %s) — the CSV in exports/ "
                  "still has this run", tab_title, type(err).__name__, err)
        return 0


def do_not_contact(tab_title: str) -> set:
    """Emails marked do_not_contact in the sheet. The only value read back, and
    it can only ever suppress a send. Empty set if unreadable — which fails
    OPEN, so callers must still check the real suppression list in the DB."""
    spreadsheet = _open_spreadsheet()
    if spreadsheet is None or "do_not_contact" not in SHEET_COLUMNS:
        return set()
    try:
        tab = spreadsheet.worksheet(tab_title)
        records = tab.get_all_records()
    except Exception as err:
        log.warning("sheets: couldn't read do_not_contact (%s)", type(err).__name__)
        return set()
    blocked = {str(r.get("email", "")).strip().lower()
               for r in records
               if str(r.get("do_not_contact", "")).strip().lower()
               in ("y", "yes", "true", "1", "x")}
    blocked.discard("")
    if blocked:
        log.info("sheets: %d address(es) marked do-not-contact in %r",
                 len(blocked), tab_title)
    return blocked


def check_connection() -> str:
    """Human-readable status for the setup check. Never raises."""
    spreadsheet = _open_spreadsheet()
    if spreadsheet is None:
        return "not connected — see the log for why"
    try:
        tabs = [w.title for w in spreadsheet.worksheets()]
        return f"connected to {spreadsheet.title!r}; tabs: {', '.join(tabs)}"
    except Exception as err:
        return f"opened but unreadable ({type(err).__name__})"
