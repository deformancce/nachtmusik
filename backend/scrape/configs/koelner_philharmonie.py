"""Auto-generated config for Kölner Philharmonie — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='koelner_philharmonie',
    name='Kölner Philharmonie',
    city='Köln',
    url='https://www.koelner-philharmonie.de/de/konzerte',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/konzerte/[a-z0-9-]+/\\d+',
    teaser_selector='li.event-item',
    field_selectors={'title': 'a.event-item__title', 'date': '.event-item__date-full', 'time': '.event-item__date-time', 'venue_hall': None},
    detail_selectors={'program_section': '.event-details__program, .event-intro__content', 'performers_section': '.event-details__cast, .headline-module__title'},
    default_venue='Kölner Philharmonie',
    default_city='Köln',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes="Single-venue site (Kölner Philharmonie). Cookie consent modal appears on load but content is rendered server-side (Nuxt SSR), so static loading works. Events are grouped by month in .event-group sections. Each event is a <li class='event-item'> with date in .event-item__date-full (h2), time in .event-item__date-time, and title in a.event-item__title. Some events occur at off-site venues (e.g. 'Im Veedel' series at Bürgerzentrum locations) — venue_hall could be scraped from a location text above the title link in those cases. Detail page has program works and performers in separate sections under 'Über diese Veranstaltung'. No pagination observed — all events appear to load on a single scrollable page. No JSON-LD structured data detected.",
    confidence=0.9,
)
