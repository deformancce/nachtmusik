"""Auto-generated config for Alte Oper Frankfurt — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='alte_oper_frankfurt',
    name='Alte Oper Frankfurt',
    city='Frankfurt',
    url='https://www.alteoper.de/de/programm',
    tier=1,
    load_method='spa',
    needs_networkidle=True,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/de/programm/[a-z0-9-]+/\\d+',
    teaser_selector='div.event-item',
    field_selectors={'title': 'a.event-item__headline .link__text', 'date': 'div.event-time span.fw-500', 'time': 'div.event-time span:nth-child(2)', 'venue_hall': 'div.event-time span:nth-child(3)'},
    detail_selectors={'program_section': None, 'performers_section': 'div.event-cast__list'},
    default_venue='Alte Oper Frankfurt',
    default_city='Frankfurt',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=False,
    notes='Site is a Nuxt.js SPA; content is rendered client-side so networkidle is required. Cookie consent modal appears on load but does not block scraping after JS renders. The listing page shows all events in a flat chronological list with no pagination or load-more button - all events appear to be rendered at once. Some events link to external ticket providers (frankfurtticket.de, hr-sinfonieorchester.de) indicating cross-promotion risk. Date, time, and venue are all within the same div.event-time element as sequential spans. Detail page has cast section under div.event-cast with performer names (event-cast__name) and roles (event-cast__role). No JSON-LD structured data detected. Event URLs follow pattern: /de/programm/[slug]/[numeric-id].',
    confidence=0.85,
)
