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
    scroll_max=30,
    scroll_wait_s=2.0,
    click_keywords=('Weitere Veranstaltungen laden',),
    event_url_pattern='/veranstaltung/[a-z0-9-]+/?$',
    teaser_selector='.tx-csconcerts-pi1 .concert-item, article.concert, .event-item',
    field_selectors={'title': 'h2 a, h3 a, .concert-title a', 'date': ".date, time, [class*='date']", 'time': "[class*='time'], .uhrzeit", 'venue_hall': "[class*='location'], [class*='venue'], [class*='saal']"},
    detail_selectors={'program_section': ".concert-program, [class*='program'], [class*='werke']", 'performers_section': ".concert-performers, [class*='performer'], [class*='artist'], [class*='mitwirkende']"},
    default_venue='Gewandhaus Leipzig',
    default_city='Leipzig',
    hall_mappings={},
    cross_promotion_risk='high',
    multi_date_per_event=False,
    notes="The listing page shows events from multiple venues including Gewandhaus, Opernhaus (Oper Leipzig), Alte Oper Frankfurt (guest concerts), etc. Cross-promotion risk is high due to third-party organizer events (e.g. Concertbüro Zahlmann, MAWI Concert GmbH, Oper Leipzig). A 'Weitere Veranstaltungen laden' click-to-load-more button is present. The actual HTML class names could not be confirmed from the Markdown rendering - CSS selectors are best guesses and require manual verification against the live DOM. Cookie consent wall present but does not block content. The detail page appears to be essentially the same template as the expanded teaser on the listing page. ICS calendar files are available per event at /veranstaltung/{slug}/konzert.ics which could be used as an alternative structured source.",
    confidence=0.45,
)
