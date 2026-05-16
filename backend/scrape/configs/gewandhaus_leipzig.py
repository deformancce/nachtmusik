"""Auto-generated config for Gewandhaus Leipzig — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='gewandhaus_leipzig',
    name='Gewandhaus Leipzig',
    city='Leipzig',
    url='https://www.gewandhausorchester.de/spielplan/',
    tier=1,
    load_method='click',
    needs_networkidle=True,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=('Weitere Veranstaltungen laden',),
    event_url_pattern='/veranstaltung/[a-z0-9-]+',
    teaser_selector='article.concert-teaser, .concert-teaser, .csconcerts-list-item',
    field_selectors={'title': '.concert-teaser__title, h2 a, .csconcerts-list-item__title', 'date': '.concert-teaser__date, time, .csconcerts-list-item__date', 'time': '.concert-teaser__time, .csconcerts-list-item__time', 'venue_hall': '.concert-teaser__location, .csconcerts-list-item__location'},
    detail_selectors={'program_section': '.concert-detail__program, .concert-detail__works', 'performers_section': '.concert-detail__performers, .concert-detail__cast'},
    default_venue='Gewandhaus Leipzig',
    default_city='Leipzig',
    hall_mappings={},
    cross_promotion_risk='high',
    multi_date_per_event=False,
    notes="The listing at https://www.gewandhausorchester.de/spielplan/ uses a 'Weitere Veranstaltungen laden' button (javascript:;) for lazy loading more events. The HTML snippet provided was truncated before reaching the main event list area — the actual teaser class names (concert-teaser etc.) could not be confirmed from the HTML and are best guesses based on TYPO3 CMS conventions used by this site. The listing includes third-party events (e.g. Oper Leipzig, Concertbüro Zahlmann, MAWI Concert GmbH) and even guest performances in other cities (Frankfurt), making cross-promotion risk high. The detail page URL pattern is /veranstaltung/[slug]-[id]/. There is no JSON-LD structured data visible. Cookie consent banner is present but appears non-blocking for scraping. The site uses TYPO3 with a custom concert plugin (tx_csconcerts). Real CSS class names for teasers need to be confirmed by inspecting the rendered main content area.",
    confidence=0.4,
)
