"""Auto-generated config for Glocke Bremen — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='glocke_bremen',
    name='Glocke Bremen',
    city='Bremen',
    url='https://www.glocke.de/programm',
    tier=1,
    load_method='scroll',
    needs_networkidle=False,
    scroll_max=40,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/programm/[a-z0-9_-]+',
    teaser_selector='.event, .veranstaltung, .programm-item',
    field_selectors={'title': 'h2, h3, .title', 'date': '.date, time', 'time': '.time', 'venue_hall': '.hall'},
    detail_selectors={'program_section': '.programm, .program', 'performers_section': '.mitwirkende'},
    default_venue='Glocke Bremen',
    default_city='Bremen',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='Manually configured',
    confidence=0.55,
)
