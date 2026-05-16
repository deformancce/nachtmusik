"""Auto-generated config for Philharmonie Essen — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='philharmonie_essen',
    name='Philharmonie Essen',
    city='Essen',
    url='https://www.theater-essen.de/philharmonie/spielplan',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/programm/kalender/\\d{4}-\\d{2}/\\?scheduleScrollTo=\\d{4}-\\d{2}-\\d{2}-p\\d+',
    teaser_selector=".schedule-item, .event-teaser, [class*='schedule'], [class*='event']",
    field_selectors={'title': '.event-title, .schedule-item__title, h3', 'date': '.event-date, .schedule-item__date, time', 'time': '.event-time, .schedule-item__time', 'venue_hall': '.event-venue, .schedule-item__venue'},
    detail_selectors={'program_section': ".program-section, .event-program, [class*='program']", 'performers_section': ".performers, .cast, [class*='performer'], [class*='cast']"},
    default_venue='Philharmonie Essen',
    default_city='Essen',
    hall_mappings={},
    cross_promotion_risk='high',
    multi_date_per_event=False,
    notes='The originally specified listing URL (https://www.theater-essen.de/philharmonie/spielplan) returns a 404. The actual calendar is at https://www.theater-essen.de/programm/kalender/ with monthly sub-pages (e.g. /programm/kalender/2025-09/). The calendar mixes events from multiple venues (Musiktheater, Ballett, Schauspiel, Philharmoniker, Philharmonie), so cross_promotion_risk is high. To filter for Philharmonie Essen only, use /programm/kalender/philharmonie-essen/. There is a cookie consent wall. Event URLs use query parameter pattern ?scheduleScrollTo=YYYY-MM-DD-pNNNN. The detail page HTML was not available (returned the calendar page instead), so CSS selectors for teasers and detail sections are guesses requiring manual review. Month pagination navigates via URL changes.',
    confidence=0.35,
)
