"""Auto-generated config for Konzerthaus Dortmund — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='konzerthaus_dortmund',
    name='Konzerthaus Dortmund',
    city='Dortmund',
    url='https://www.konzerthaus-dortmund.de/de/programm/',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/programm/\\d{2}-\\d{2}-\\d{4}-[a-z0-9-]+/',
    teaser_selector='li.va',
    field_selectors={'title': '.card-text .h2', 'date': '.timebox .list-inline-item:first-child', 'time': '.timebox .list-inline-item:nth-child(2)', 'venue_hall': None},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Konzerthaus Dortmund',
    default_city='Dortmund',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes="All events are rendered server-side in a single <ul id='cal-list'> list. Each event is a <li class='va ...'> with genre classes (e.g. 'musikdialog', 'khd', 'weekend'). The timebox contains date and time as separate list items. Detail URLs follow pattern /de/programm/DD-MM-YYYY-slug/. The listing page supports URL-based filtering via query params (d=timestamp, e=true, g=, w=false). No pagination or lazy loading observed - all events are in the initial HTML. Some events include third-party tickets (theaterdo.de) but the events themselves are Konzerthaus Dortmund events. No JSON-LD structured data detected. Banner/ad items appear within the list but lack the 'va' class so can be filtered by the teaser selector.",
    confidence=0.85,
)
