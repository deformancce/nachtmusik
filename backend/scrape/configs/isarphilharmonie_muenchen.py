"""Auto-generated config for Isarphilharmonie München — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='isarphilharmonie_muenchen',
    name='Isarphilharmonie München',
    city='München',
    url='https://www.mphil.de/kalender',
    tier=0,
    load_method='static',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/konzerte-und-karten/kalender/konzerte/[a-z0-9-]+',
    teaser_selector='li.m-mphil-concertlist__item',
    field_selectors={'title': '.m-mphil-concertlist__headline', 'date': 'time.m-mphil-concertlist__date', 'time': 'time.m-mphil-concertlist__date', 'venue_hall': '.m-mphil-concertlist__venue'},
    detail_selectors={'program_section': 'ul.m-mphil-concert-detail__work-list', 'performers_section': 'ul.m-mphil-concert-detail__person-list'},
    default_venue='Isarphilharmonie München',
    default_city='München',
    hall_mappings={},
    cross_promotion_risk='high',
    multi_date_per_event=False,
    notes="The listing page at https://www.mphil.de/kalender contains all events in a single scrollable list grouped by month. Each event is an <li class='m-mphil-concertlist__item'>. The page includes guest concerts (Gastkonzerte) at external venues worldwide (KKL Luzern, Philharmonie Essen, Concertgebouw Amsterdam, etc.) alongside home venue Isarphilharmonie events — cross_promotion_risk is high because filtering by venue is needed to isolate Isarphilharmonie events. The venue field appears twice per card (one hidden on small screens), use .m-mphil-concertlist__venue.--hide-small-mobile-down for desktop. Date and time are combined in the <time> element (e.g. 'Di. 09.09.2025, 19:30 Uhr'). Detail URLs follow pattern /konzerte-und-karten/kalender/konzerte/{slug}. The month selector is a <select> element but all months appear to be rendered in the page DOM (static). No cookie wall or anti-bot measures observed. No JSON-LD structured data found.",
    confidence=0.88,
)
