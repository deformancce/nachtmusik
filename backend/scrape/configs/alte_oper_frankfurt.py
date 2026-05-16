"""Auto-generated config for Alte Oper Frankfurt — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='alte_oper_frankfurt',
    name='Alte Oper Frankfurt',
    city='Frankfurt',
    url='https://www.alteoper.de/de/programm',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/(veranstaltung|event|programm)/[a-z0-9_-]+',
    teaser_selector='.event, .program-item, .concert-item',
    field_selectors={'title': 'h2, h3, .event-title, .title', 'date': 'time, .date, .event-date', 'time': '.time, .event-time', 'venue_hall': '.hall, .venue, .location'},
    detail_selectors={'program_section': '.program, #programm', 'performers_section': '.performers, .cast'},
    default_venue='Alte Oper Frankfurt',
    default_city='Frankfurt',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='Manually configured — run analyze_venues.py --only alte_oper_frankfurt with ANTHROPIC_API_KEY to auto-refine',
    confidence=0.6,
)
