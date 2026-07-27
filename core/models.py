"""
Shared data shapes, kept here so storage, collector, and extractor can all use
them without importing each other (avoids tangled cross-imports).

  Listing — a business found in the results feed (name + URL + dedupe key),
            before we've opened it.
  Lead    — a fully extracted business, ready for the spreadsheet.
"""

from dataclasses import dataclass


@dataclass
class Listing:
    name: str
    url: str
    place_key: str


@dataclass
class Lead:
    place_key: str
    name: str
    category: str | None
    address: str | None
    phone: str | None
    website: str | None
    rating: float | None
    reviews: int | None
    maps_url: str
    query: str
    # Milestone 9 — richer place fields (any may be None; e.g. price is rare
    # for service businesses).
    open_state: str | None = None    # Open | Closed | Permanently closed | ...
    hours: str | None = None         # weekly summary, e.g. "Mon 8 AM-5 PM; ..."
    price_level: str | None = None   # "$$", "Moderate", "$10-20", ...
    plus_code: str | None = None     # e.g. "F6XG+W5"
