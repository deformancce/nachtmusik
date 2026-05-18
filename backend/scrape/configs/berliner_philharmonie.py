"""Auto-generated config for Berliner Philharmonie — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='berliner_philharmonie',
    name='Berliner Philharmonie',
    city='Berlin',
    url='https://www.berliner-philharmoniker.de/konzerte',
    tier=0,
    load_method='static',
    needs_networkidle=False,
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/konzerte/kalender/[a-z0-9-]+',
    teaser_selector='.card__grid .card',
    field_selectors={'title': '.card__title', 'date': None, 'time': None, 'venue_hall': None},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Berliner Philharmonie',
    default_city='Berlin',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=False,
    notes='The URL https://www.berliner-philharmoniker.de/konzerte is a landing/hub page, NOT an event listing. It contains navigation cards linking to sub-sections (Kalender, Abos, Ticketinfo, etc.) rather than individual concert teasers with dates and times. The actual event calendar is at https://www.berliner-philharmoniker.de/konzerte/kalender/ and likely uses a SPA or dynamic rendering for the event list. The scraper should target the Kalender page instead. The listing page also includes Gastveranstaltungen (guest events) from external promoters, raising cross-promotion risk. No event-level date, time, or venue fields are visible in the provided HTML. Confidence is low because the provided page does not contain actual event listings.',
    confidence=0.15,
)
