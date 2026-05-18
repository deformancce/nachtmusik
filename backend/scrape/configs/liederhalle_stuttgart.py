"""Auto-generated config for Liederhalle Stuttgart — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='liederhalle_stuttgart',
    name='Liederhalle Stuttgart',
    city='Stuttgart',
    url='https://liederhalle.de/eventkalender',
    tier=0,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/veranstaltung/[a-z0-9-]+',
    teaser_selector='div.bb_datacard__item',
    field_selectors={'title': 'h3.bb_datacard__title', 'date': 'li.bb_datacard__list-item:nth-child(1) span.bb_datacard__list-data', 'time': 'li.bb_datacard__list-item:nth-child(2) span.bb_datacard__list-data', 'venue_hall': 'li.bb_datacard__list-item:nth-child(3) span.bb_datacard__list-data'},
    detail_selectors={'program_section': 'div.eventkalender--detail div.typography', 'performers_section': 'div.bb_datacard__wrapper ul.bb_datacard__list'},
    default_venue='Liederhalle Stuttgart',
    default_city='Stuttgart',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes="Site is a TYPO3 CMS. All events are hosted at Liederhalle Stuttgart (low cross-promotion risk). Liederhalle is not the organizer - it rents out space. Pagination appears to be handled via URL parameter tx_bbevents_events[arguments][currentPage]. The listing loads statically - no infinite scroll or JS-loaded content observed. A cookie consent banner appears but does not block content. Date format on listing is 'Sonntag, 17.5.2026', on detail page '17.05.2026'. The field selectors for date/time/venue use nth-child but the list items are labeled with bb_datacard__list-title spans containing 'Datum', 'Uhrzeit', 'Saal / Raum' - scraper should match by label text for robustness. Detail page URL pattern: /veranstaltung/[slug]. Similar events section on detail page may list events from other dates - scrape only the main event info block.",
    confidence=0.88,
)
