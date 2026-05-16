"""Auto-generated config for Philharmonie Essen — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='philharmonie_essen',
    name='Philharmonie Essen',
    city='Essen',
    url='https://www.theater-essen.de/programm/kalender/philharmonie-essen/',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/programm/[a-z0-9-]+/p[0-9]+',
    teaser_selector='.scheduleitem',
    field_selectors={'title': '.scheduleitem__title', 'date': '.scheduleitem__date', 'time': '.scheduleitem__time', 'venue_hall': '.scheduleitem__location'},
    detail_selectors={'program_section': '.performancedetail__program', 'performers_section': '.performancedetail__cast'},
    default_venue='Philharmonie Essen',
    default_city='Essen',
    hall_mappings={},
    cross_promotion_risk='low',
    multi_date_per_event=False,
    notes="The listing page is a calendar-based schedule filtered for 'Philharmonie Essen'. The URL pattern for filtering by venue is /programm/kalender/YYYY-MM/philharmonie-essen/. The page shows a month-by-month calendar; to scrape all events, iterate over monthly URLs like /programm/kalender/2025-09/philharmonie-essen/, /programm/kalender/2025-10/philharmonie-essen/, etc. The HTML snippet provided does not include the actual event teaser blocks (scheduleitem elements) as the main content area was cut off — CSS selectors for teasers are inferred from typical TUP site patterns. There is a cookie consent wall but it appears to be client-side only and should not block static HTML scraping. No JSON-LD structured data was observed. The detail page URL shown is actually still the calendar page with a query parameter (scheduleScrollTo), not a true detail page URL — real event detail pages likely follow a pattern like /programm/veranstaltungen/[slug]/p[id]. Cross-promotion risk is low since the filter is specifically for Philharmonie Essen. Pagination is handled via monthly URL segments, not a 'load more' button.",
    confidence=0.35,
)
