"""
Vertical profiles — the file that makes the gate personal rather than generic.

Everything the gate needs to judge one trade lives in profiles/verticals.json:
what we sell them, what a relevant complaint sounds like, which chains to skip,
which accreditations prove they're real, and worked examples of a good and bad
fit. The gate code reads a profile; it contains no per-trade logic of its own.

Adding a vertical is adding a JSON block. If you find yourself writing an
`if vertical == ...` anywhere in qualifier/, the profile is missing a field.
"""

import json
from functools import lru_cache

from config import BASE_DIR
from core.logbook import get_logger

log = get_logger(__name__)

PROFILES_PATH = BASE_DIR / "profiles" / "verticals.json"

# Fields merged down from `shared` unless the vertical overrides them.
_INHERITED = ("gate", "vendor_email_domains", "quality_complaint_terms",
              "maps_categories_exclude_global", "global_chains_exclude",
              "offers", "national_phone_prefixes")


@lru_cache(maxsize=1)
def _load() -> dict:
    """The whole profiles file, parsed once."""
    try:
        with open(PROFILES_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        log.error("no vertical profiles at %s", PROFILES_PATH)
    except json.JSONDecodeError as err:
        log.error("vertical profiles are not valid JSON (%s)", err)
    return {"shared": {}, "verticals": []}


def slug(name: str) -> str:
    """'Boiler & Gas / Heating Engineer' -> 'boiler-gas-heating-engineer'.
    Used for --vertical arguments and DB values; the readable name is what
    reaches the spreadsheet and the UI."""
    kept = [c.lower() if c.isalnum() else "-" for c in (name or "")]
    return "-".join(part for part in "".join(kept).split("-") if part)


def names() -> list:
    """Readable vertical names, in the ranked order the file gives them."""
    ordered = sorted(_load().get("verticals", []), key=lambda v: v.get("rank", 999))
    return [v["vertical"] for v in ordered]


def name_for(vertical: str) -> str:
    """The readable name for a slug — 'emergency-plumbing' -> 'Emergency
    Plumbing'. Anything the operator reads (tab titles, spreadsheet cells)
    should go through this, or the same vertical ends up looking like two."""
    wanted = slug(vertical)
    for profile in _load().get("verticals", []):
        if slug(profile.get("vertical", "")) == wanted:
            return profile["vertical"]
    return vertical or ""


def load(vertical: str) -> dict | None:
    """One profile with `shared` merged in, or None (logged) if unknown.
    Accepts either the readable name or its slug."""
    data = _load()
    wanted = slug(vertical)
    for profile in data.get("verticals", []):
        if slug(profile.get("vertical", "")) != wanted:
            continue
        merged = dict(profile)
        shared = data.get("shared", {})
        for field in _INHERITED:
            merged.setdefault(field, shared.get(field))
        merged["slug"] = wanted
        # One list to check names against, rather than two at every call site.
        merged["chains"] = ([*(profile.get("chains_to_exclude") or []),
                             *(shared.get("global_chains_exclude") or [])])
        merged["categories_exclude"] = (
            [*(profile.get("maps_categories_exclude") or []),
             *(shared.get("maps_categories_exclude_global") or [])])
        merged["cities"] = shared.get("cities_wave_1", [])
        return merged
    log.error("unknown vertical %r — known: %s", vertical, ", ".join(names()))
    return None


def queries(vertical: str, cities: list | None = None) -> list:
    """Every search string for a vertical: each term across each city."""
    profile = load(vertical)
    if profile is None:
        return []
    return [f"{term} in {city}"
            for city in (cities or profile["cities"])
            for term in profile.get("search_terms", [])]
