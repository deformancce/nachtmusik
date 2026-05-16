"""VenueConfig dataclass — the single source of truth for a venue's scraping parameters."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class VenueConfig:
    # Identity
    slug: str                  # "konzerthaus_berlin" — used for file/workflow names
    name: str                  # "Konzerthaus Berlin"
    city: str                  # "Berlin"
    url: str                   # listing page URL
    tier: int = 1

    # How to render the listing page
    load_method: Literal["scroll", "click", "static", "spa", "jsonld", "ical"] = "scroll"
    scroll_max: int = 60
    scroll_wait_s: float = 2.0
    click_keywords: tuple[str, ...] = ()  # button texts to click for "load more"
    needs_networkidle: bool = False        # True for heavy SPAs

    # CSS selectors for listing page (used by JsonCssExtractionStrategy)
    event_url_pattern: str = ""  # regex to identify event detail links
    teaser_selector: str = ""    # CSS base selector for one event teaser
    field_selectors: dict = field(default_factory=dict)  # title, date, venue, etc.

    # Detail page selectors
    detail_selectors: dict = field(default_factory=dict)  # program_section, performers

    # Venue-specific overrides
    default_venue: str = ""
    default_city: str = ""
    hall_mappings: dict = field(default_factory=dict)  # hall_name → (venue, city)
    multi_date_per_event: bool = False
    cross_promotion_risk: str = "low"  # low / medium / high

    # Extra instruction fragment appended to the Claude system prompt
    claude_hint: str = ""

    # Metadata
    notes: str = ""
    confidence: float = 1.0
