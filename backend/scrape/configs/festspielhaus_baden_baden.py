"""Auto-generated config for Festspielhaus Baden-Baden — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='festspielhaus_baden_baden',
    name='Festspielhaus Baden-Baden',
    city='Baden-Baden',
    url='https://www.festspielhaus.de/spielplan',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/veranstaltungen?/[a-z0-9_-]+',
    teaser_selector='.event, .performance, .spielplan-item',
    field_selectors={'title': 'h2, h3, .title', 'date': '.date, time', 'time': '.time', 'venue_hall': '.hall, .location'},
    detail_selectors={'program_section': '.programm, .program', 'performers_section': '.mitwirkende, .kuenstler'},
    default_venue='Festspielhaus Baden-Baden',
    default_city='Baden-Baden',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='Manually configured',
    confidence=0.55,
)
