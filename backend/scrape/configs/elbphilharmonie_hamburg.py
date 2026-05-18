"""Auto-generated config for Elbphilharmonie Hamburg — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='elbphilharmonie_hamburg',
    name='Elbphilharmonie Hamburg',
    city='Hamburg',
    url='https://www.elbphilharmonie.de/de/programm/',
    tier=0,
    load_method='static',
    needs_networkidle=True,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/programm/[a-z0-9-]+/\\d+$',
    teaser_selector='section#program-section ul.booking-list li',
    field_selectors={'title': "a.booking-list__link, li > a:not([class*='ticket'])", 'date': 'li > strong:first-child, .booking-list__date', 'time': 'li > strong:first-child', 'venue_hall': 'li > strong:nth-child(2), .booking-list__location'},
    detail_selectors={'program_section': '.event-detail__program, .booking-detail__description', 'performers_section': '.event-detail__cast, .booking-detail__performers'},
    default_venue='Elbphilharmonie Hamburg',
    default_city='Hamburg',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=True,
    notes="The listing page is at https://www.elbphilharmonie.de/de/programm/ and shows events for both Elbphilharmonie and Laeiszhalle (two venues). The HTML snippet does not reveal the exact CSS classes for individual event teaser list items - the booking list is rendered inside section#program-section.booking-list-page but the actual <ul> and <li> class names for event rows are not visible in the provided snippet (likely rendered by JS or truncated). The page has a cookie consent wall that must be dismissed. Events appear as <li> items in a flat list with date/time in bold text and venue on the next line. Some events show multiple times (e.g. '16 Uhr / 18:30 Uhr'). Event URLs follow pattern /de/programm/[slug]/[numeric-id]. The site also includes events at external/satellite venues (Zeise Kinos, Bunker Feldstraße, etc.) which adds medium cross-promotion risk. No JSON-LD structured data detected. The list loads statically but a JS-heavy SPA behavior is present for filtering. Scroll may be needed to load all events.",
    confidence=0.45,
)
