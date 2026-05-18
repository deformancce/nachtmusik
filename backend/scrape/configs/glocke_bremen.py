"""Auto-generated config for Glocke Bremen — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='glocke_bremen',
    name='Glocke Bremen',
    city='Bremen',
    url='https://www.glocke.de/tickets-programm/',
    tier=0,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/event/[a-z0-9-]+/',
    teaser_selector='article.event__single__wrapper',
    field_selectors={'title': 'h2.tribe-events-calendar-list__event-title a', 'date': 'div.event__date_box p', 'time': 'div.event__time__holder h4', 'venue_hall': 'div.event__time__holder h4'},
    detail_selectors={'program_section': 'div.column_attr.custom__column__all__padding', 'performers_section': 'div#accordion__single__shortcode'},
    default_venue='Glocke Bremen',
    default_city='Bremen',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes="Listing uses WordPress with The Events Calendar plugin (tribe). Events are loaded statically on paginated pages (/tickets-programm/page/2/ etc.). Time and venue hall are combined in the same h4 element (e.g. '20:00 Uhr | Kleiner Saal') — split on ' | ' to separate them. Date box contains weekday name, day number, and month name as separate spans/b elements. Detail pages use back_btn=1 query param but the canonical URL without it works fine. No cookie wall observed. Cross-promotion risk is low as all events are hosted at Glocke Bremen venues (Großer Saal, Kleiner Saal, Foyer). Pagination via standard WordPress page URLs.",
    confidence=0.85,
)
