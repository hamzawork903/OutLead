"""
Upgrade a raw vertical file to what the gate needs, in place.

Run this after pasting the source JSON over verticals.json. It adds the few
fields the gate reads that the source file doesn't carry, and leaves every
number you wrote exactly as you wrote it — nothing here edits avg_job_value_gbp
or any other figure, because those drive real claims in real emails.

    python profiles/adapt.py

Safe to run twice: it only fills in what's missing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logbook import get_logger, setup_logging   # noqa: E402

log = get_logger(__name__)

PATH = Path(__file__).parent / "verticals.json"

# Trade bodies per vertical. A membership number on their site is harder proof
# the business is real and established than any review count.
ACCREDITATIONS = {
    "Emergency Plumbing": ["gas safe", "watersafe", "ciphe", "aphc"],
    "Boiler & Gas / Heating Engineer": ["gas safe", "oftec", "hetas", "ciphe"],
    "Water Damage & Flood Restoration": ["iicrc", "bdma", "napit"],
    "Emergency Electrician": ["niceic", "napit", "elecsa", "stroma"],
    "Drainage & Blocked Drains": ["nadc", "watersafe", "ciphe"],
    "Roofing & Emergency Roof Repair": ["nfrc", "confederation of roofing", "trustmark"],
    "Air Conditioning / HVAC": ["f-gas", "refcom", "reci", "besa"],
    "Locksmith": ["mla", "master locksmiths"],
    "Garage Doors": ["dhf", "door and hardware federation"],
    "Pest Control": ["bpca", "npta", "basis prompt"],
    "Private Dental Practice": ["gdc", "cqc", "bda"],
    "Cosmetic & Implant Dentistry": ["gdc", "cqc", "bacd", "ada"],
    "Med Spa / Aesthetics Clinic": ["cqc", "save face", "jccp", "nmc"],
    "Physiotherapy / Chiropractic / Osteopathy": ["hcpc", "csp", "gcc", "gosc"],
    "Personal Injury Solicitors": ["sra", "law society", "apil"],
    "Windows, Doors & Conservatories": ["fensa", "certass", "ggf", "trustmark"],
    "Solar & Battery Installation": ["mcs", "recc", "hies", "niceic"],
    "Emergency Glazing / Boarding Up": ["ggf", "fensa", "myglaziers"],
    "Skip Hire & Waste Clearance": ["waste carrier", "environment agency", "ciwm"],
    "Removals & Man With A Van": ["bar", "british association of removers", "fidi"],
}

# Gate settings the source file doesn't carry.
SHARED_ADDITIONS = {
    "vendor_email_domains": [
        "moatable.com", "webform.boxly.ai", "indiantypefoundry.com",
        "sentry.io", "wixpress.com", "godaddy.com", "squarespace.com",
    ],
    # Extended from the source list with the billing vocabulary that dominated
    # a real dentist run — the original list was written for trades and missed
    # every one of these.
    "quality_complaint_terms": [
        "overcharged", "over charged", "rip off", "ripoff", "scam",
        "damaged", "damage to", "botched", "shoddy", "cowboy",
        "left a mess", "made it worse", "had to redo", "poor workmanship",
        "incompetent", "dangerous", "not qualified", "illegal",
        "took my money", "never finished", "walked off the job",
        "deceptive billing", "surprise bill", "out of network",
        "unnecessary", "upsell", "prepay", "hidden charges",
    ],
}

GATE_ADDITIONS = {
    "quality_ratio_min_sample": 4,     # one bad review can't condemn a business
    "structural_signals_required": 2,  # one signal would let everything through
}


def main() -> int:
    setup_logging("profiles-adapt")
    if not PATH.exists():
        log.error("no file at %s — paste your vertical JSON there first", PATH)
        return 1
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        log.error("verticals.json is not valid JSON (%s)", err)
        return 1

    shared = data.setdefault("shared", {})
    for key, value in SHARED_ADDITIONS.items():
        shared.setdefault(key, value)
    for key, value in GATE_ADDITIONS.items():
        shared.setdefault("gate", {}).setdefault(key, value)

    verticals = data.get("verticals", [])
    added, unknown = 0, []
    for profile in verticals:
        name = profile.get("vertical", "")
        if profile.get("accreditations"):
            continue
        bodies = ACCREDITATIONS.get(name)
        if bodies:
            profile["accreditations"] = bodies
            added += 1
        else:
            unknown.append(name)

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log.info("%d vertical(s) in file; accreditations added to %d",
             len(verticals), added)
    if unknown:
        log.warning("no accreditation list for: %s — add one or the "
                    "'is this a real firm' signal can't fire for them",
                    ", ".join(unknown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
