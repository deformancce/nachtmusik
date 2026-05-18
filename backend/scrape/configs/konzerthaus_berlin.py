"""Auto-generated config for Konzerthaus Berlin — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='konzerthaus_berlin',
    name='Konzerthaus Berlin',
    city='Berlin',
    url='https://www.konzerthaus.de/de/programm',
    tier=0,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/programm/[a-z0-9äöü-]+/\\d+',
    teaser_selector='li.event-item',
    field_selectors={'title': 'p.copy', 'date': 'h3.event-title', 'time': 'h3.event-title', 'venue_hall': None},
    detail_selectors={'program_section': 'div.event-summary', 'performers_section': 'div.event-summary'},
    default_venue='Konzerthaus Berlin',
    default_city='Berlin',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=False,
    notes='The listing page is a calendar-based view at https://www.konzerthaus.de/de/programm organized by month. Each day has a URL like /de/programm/DD-MM-YYYY. Individual event detail pages follow the pattern /de/programm/event-slug/NNNNN (e.g. /de/programm/vogler-quartett/11530). The scraper should iterate through daily URLs to collect all events, as the main listing is calendar-based not a flat list. The listing also includes guest performances at external venues (Elbphilharmonie Hamburg, Philharmonie Essen, etc.) which increases cross-promotion risk slightly. The detailed event list on the current-day page shows events as li.event-item elements with h3.event-title for time and p.copy for event name. No JSON-LD structured data detected. No apparent anti-bot measures observed. Navigation between months uses javascript:; links so month navigation may require JS interaction, but individual day pages are static.',
    confidence=0.72,
)
