"""Auto-generated config for Kölner Philharmonie — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='koelner_philharmonie',
    name='Kölner Philharmonie',
    city='Köln',
    url='https://www.koelner-philharmonie.de/de/programm',
    tier=1,
    load_method='scroll',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=('Mehr laden', 'Weitere Konzerte', 'Mehr anzeigen'),
    event_url_pattern='/de/(veranstaltung|konzert|event)/[a-z0-9_-]+',
    teaser_selector='.event-teaser, .concert-item, .program-entry',
    field_selectors={'title': 'h2, h3, .title, .event-title', 'date': 'time, .date', 'time': '.time', 'venue_hall': '.hall, .location'},
    detail_selectors={'program_section': '.programm, .program, #programm-section', 'performers_section': '.mitwirkende, .artists, .solisten'},
    default_venue='Kölner Philharmonie',
    default_city='Köln',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='Manually configured — run analyze_venues.py --only koelner_philharmonie with ANTHROPIC_API_KEY',
    confidence=0.6,
)
