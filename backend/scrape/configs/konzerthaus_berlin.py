"""Auto-generated config for Konzerthaus Berlin — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='konzerthaus_berlin',
    name='Konzerthaus Berlin',
    city='Berlin',
    url='https://www.konzerthaus.de/programm',
    tier=1,
    load_method='scroll',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/veranstaltung/[a-z0-9_-]+',
    teaser_selector='.event-item, .c-event-teaser, article.event',
    field_selectors={'title': 'h2, h3, .event-title', 'date': 'time, .event-date, .date', 'time': '.event-time, .time', 'venue_hall': '.event-venue, .hall'},
    detail_selectors={'program_section': '.program, .programm, #programm', 'performers_section': '.performers, .mitwirkende, .artists'},
    default_venue='Konzerthaus Berlin',
    default_city='Berlin',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='Manually configured — run analyze_venues.py --only konzerthaus_berlin with ANTHROPIC_API_KEY to auto-refine selectors',
    confidence=0.6,
)
