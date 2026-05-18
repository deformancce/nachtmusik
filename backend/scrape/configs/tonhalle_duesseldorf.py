"""Auto-generated config for Tonhalle Düsseldorf — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug='tonhalle_duesseldorf',
    name='Tonhalle Düsseldorf',
    city='Düsseldorf',
    url='https://www.tonhalle.de/veranstaltungen/kalender',
    tier=0,
    load_method='scroll',
    needs_networkidle=True,
    scroll_max=60,
    scroll_wait_s=2.0,
    click_keywords=(),
    event_url_pattern='/veranstaltung/[^/]+/[0-9]+-[a-z0-9-]+',
    teaser_selector='.grid > div',
    field_selectors={'title': '.tile-desktop__title span, .absolute.left-0.bottom-0 .text-base.leading-tight', 'date': 'ul.inline-flex li span, span.inline-block.text-\\[16px\\].md\\:hidden span', 'time': None, 'venue_hall': None},
    detail_selectors={'program_section': None, 'performers_section': None},
    default_venue='Tonhalle Düsseldorf',
    default_city='Düsseldorf',
    hall_mappings={},
    cross_promotion_risk='medium',
    multi_date_per_event=True,
    notes="This is a Next.js SPA (id='__next'). The calendar page at /veranstaltungen/kalender shows events in a grid. Teasers are inside a '.grid > div' structure. Desktop tiles use class 'tile-desktop' with title in 'tile-desktop__title'. Some events have multiple dates displayed (e.g. '8.6.26 ... 9.6.26 ... 11.6.26'). The site includes guest promoter events (Komet series = Gastveranstalter), introducing medium cross-promotion risk. Detail pages show time ('20:00 Konzertbeginn') and hall ('Mendelssohn-Saal') in a list but no clear semantic CSS class names visible for those fields - they are likely plain list items. No JSON-LD structured data was observed. The page uses Tailwind CSS utility classes making stable selectors harder to pin down. Scroll-based loading is likely needed as it's a SPA calendar.",
    confidence=0.52,
)
