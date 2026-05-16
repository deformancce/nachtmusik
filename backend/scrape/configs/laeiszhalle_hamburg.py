"""Auto-generated config for Laeiszhalle Hamburg — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='laeiszhalle_hamburg',
    name='Laeiszhalle Hamburg',
    city='Hamburg',
    url='https://www.elbphilharmonie.de/de/programm/laeiszhalle',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/programm/[a-z0-9-]+',
    teaser_selector=None,
    field_selectors={'title': None, 'date': None, 'time': None, 'venue_hall': None},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Laeiszhalle Hamburg',
    default_city='Hamburg',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes='The listing page https://www.elbphilharmonie.de/de/programm/laeiszhalle returns a 404 Not Found page. The correct Laeiszhalle filter URL may differ — try https://www.elbphilharmonie.de/de/programm?venue=laeiszhalle or similar. No event data could be extracted from the provided HTML. Manual review of the correct URL is required before configuring selectors.',
    confidence=0.1,
)
