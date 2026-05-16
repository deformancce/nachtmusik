"""Auto-generated config for Festspielhaus Baden-Baden — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='festspielhaus_baden_baden',
    name='Festspielhaus Baden-Baden',
    city='Baden-Baden',
    url='https://www.festspielhaus.de/programm/',
    tier=1,
    load_method='spa',
    needs_networkidle=True,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/veranstaltungen/[a-z0-9-]+/',
    teaser_selector='div.m-quickview-event',
    field_selectors={'title': '.m-quickview-event__information .c-heading__headline', 'date': '.m-quickview-event__date', 'time': '.m-quickview-event__date span:last-child', 'venue_hall': '.m-quickview-event__attributes span'},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Festspielhaus Baden-Baden',
    default_city='Baden-Baden',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=True,
    notes='This is a Nuxt.js SPA. All events appear on a single listing page at /programm/ with no pagination or load-more button. Events are grouped under festival headers (m-quickview-festival) and as standalone list-items. Each unique performance gets its own teaser (m-quickview-event), so the same title may appear multiple times with different dates. The detail URL includes a ?date= query param (e.g. /veranstaltungen/richard-strauss-der-rosenkavalier/?date=2026-05-17-1600). Venue is in m-quickview-event__attributes and is absent if the event is at the main Festspielhaus. Events span multiple real-world venues (Kurhaus, Theater Baden-Baden, Kongresshaus, etc.), raising cross-promotion risk to medium. No JSON-LD or iCal detected. networkidle recommended to ensure Vue/Nuxt renders all list items.',
    confidence=0.85,
)
