"""Auto-generated config for Kölner Philharmonie — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='k_lner_philharmonie',
    name='Kölner Philharmonie',
    city='Köln',
    url='https://www.koelner-philharmonie.de/de/programm',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/konzerte/[a-z0-9-]+/\\d+',
    teaser_selector="ul li:has(a[href*='/de/konzerte/'])",
    field_selectors={'title': "a[href*='/de/konzerte/']", 'date': 'li > .date, li > span:first-child', 'time': None, 'venue_hall': None},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Kölner Philharmonie',
    default_city='Köln',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=False,
    notes="Cookie consent wall present (accept/decline). The listing page shows events as list items with date, time, title and performers. Date and time appear as plain text within the list item (e.g. 'Sa 16.05.2026 20:00'). Some events take place at external/offsite venues (PhilharmonieVeedel Pänz, Im Veedel tagged events) — these should be flagged or filtered. The URL pattern is /de/konzerte/{slug}/{id}. Each event has its own URL with a unique numeric ID. The page appears to be server-side rendered with all events visible in the initial HTML (static load), though JavaScript may enhance it. No JSON-LD detected from the listing page sample. Anti-bot measures not evident beyond the cookie banner. Cross-promotion risk is medium because some concerts feature external orchestras (Gürzenich-Orchester uses mandant=260) and community/outreach events at other venues.",
    confidence=0.62,
)
