"""Auto-generated config for Berliner Philharmonie — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='berliner_philharmonie',
    name='Berliner Philharmonie',
    city='Berlin',
    url='https://www.berliner-philharmoniker.de/konzerte',
    tier=1,
    load_method='static',
    needs_networkidle=False,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/konzerte/kalender/[0-9]+',
    teaser_selector='.tx-bphconcerts-pi1 .concert-teaser, article.concert, .concert-list-item',
    field_selectors={'title': 'strong, .concert-title, h2, h3', 'date': 'strong:first-child', 'time': 'strong:first-child', 'venue_hall': '.concert-location, strong + text'},
    detail_selectors={'program_section': '.concert-program, .program-section, .programm', 'performers_section': '.concert-performers, .artists, .besetzung'},
    default_venue='Berliner Philharmonie',
    default_city='Berlin',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=True,
    notes="The actual listing page is at /konzerte/kalender/ not /konzerte/. The calendar mixes Berliner Philharmoniker concerts with guest events (Gastveranstaltungen). Events with the same program appear multiple times for different dates (e.g. Mahler Symphony No. 3 appears Thu/Fri/Sat). Event detail URLs follow pattern /konzerte/kalender/{numeric_id}/. The provided markdown is rendered content from a SPA-like calendar with month navigation. Teaser blocks appear to be rendered directly in the page HTML. No JSON-LD detected. The listing page /konzerte/ is just a hub page with links, not the actual calendar - scraper should target /konzerte/kalender/. Date and time appear combined in bold text like 'Sa 16. Mai 2026, 19.00 Uhr'. Hall info appears right after the date bold text. CSS selectors are uncertain without inspecting actual DOM - manual review of real HTML needed to confirm selectors.",
    confidence=0.45,
)
