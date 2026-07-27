"""
Place identity. One job: turn a Maps listing URL into a stable dedupe key.

Google's permanent place id (ChIJ...) lives in the URL after "!19s". It never
changes for a business, so it's the ideal key for dedup and crash-resume. If a
URL somehow lacks it, we fall back to the name-path (the part before "/data=").
"""

import re

_PLACE_ID_RE = re.compile(r"!19s(ChIJ[^!?&]+)")


def place_key_from_url(url: str) -> str:
    match = _PLACE_ID_RE.search(url)
    return match.group(1) if match else url.split("/data=")[0]
