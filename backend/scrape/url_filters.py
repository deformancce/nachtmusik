"""
Event URL filtering for discovery (map) and coverage metrics.

Loose filters count anything that might be event-related (legacy behaviour).
Strict filters approximate real concert *detail* pages — closer to custom scrapers.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Broad hint — still used for loose discovery counts.
_EVENT_PATH_LOOSE_RE = re.compile(
    r"/(?:veranstaltung|veranstaltungen|event|events|konzert|konzerte"
    r"|vorstellung|aufführung|show)/[^/?#]+",
    re.IGNORECASE,
)

_EXCLUDE_RE = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|css|js|ico|mp4|mp3|xml|zip|svg|ics)(\?|$)"
    r"|/(login|register|account|suche|search|sitemap|impressum"
    r"|datenschutz|agb|newsletter|presse|press|media|shop|sponsor"
    r"|kontakt|contact|ueber|about|team|jobs|karriere"
    r"|ticketinfo|tickets|abo|abos|saison|subscription|flex)(/|$)",
    re.IGNORECASE,
)

# Listing / hub pages (not a single concert).
_LISTING_PATH_RE = re.compile(
    r"/(?:konzerte|veranstaltungen|events|programm|spielplan|kalender)"
    r"/?(?:\?|#|$)"
    r"|/(?:konzerte|programm|spielplan|kalender|veranstaltungen|events)$",
    re.IGNORECASE,
)

# Archive, pagination, categories.
_NOISE_PATH_RE = re.compile(
    r"/(?:category|tag|tags|archiv|archive|page/\d+|author/|thema/)"
    r"|/\d{4}/\d{2}/(?:\?|$)",
    re.IGNORECASE,
)

_NUMERIC_DETAIL_TAIL_RE = re.compile(r"/\d{3,}/?$")

# Per-venue detail URL shapes (aligned with working listing detail_url values).
VENUE_STRICT_PATTERNS: dict[str, re.Pattern[str]] = {
    "berliner_philharmonie": re.compile(
        r"berliner-philharmoniker\.de/konzerte/(?:kalender/\d+|[a-z0-9äöüß-]+)/?$",
        re.I,
    ),
    "koelner_philharmonie": re.compile(
        r"koelner-philharmonie\.de/de/konzerte/[^/?#]+/\d{3,}",
        re.I,
    ),
    "gewandhaus_leipzig": re.compile(
        r"gewandhausorchester\.de/veranstaltung/[^/?#]+",
        re.I,
    ),
    "konzerthaus_berlin": re.compile(
        r"konzerthaus\.de/de/programm/[^/?#]+/\d+",
        re.I,
    ),
    "elbphilharmonie_hamburg": re.compile(
        r"elbphilharmonie\.de/.+/(?:veranstaltung|event)/[^/?#]+",
        re.I,
    ),
    "alte_oper_frankfurt": re.compile(
        r"alteoper\.de/.+/\d{4,}",
        re.I,
    ),
    "konzerthaus_dortmund": re.compile(
        r"konzerthaus-dortmund\.de/de/programm/\d{2}-\d{2}-\d{4}-[^/?#]+",
        re.I,
    ),
    "tonhalle_duesseldorf": re.compile(
        r"tonhalle\.de/.+veranstaltung[^/?#]*",
        re.I,
    ),
    "philharmonie_essen": re.compile(
        r"theater-essen\.de/.+p\d{3,}",
        re.I,
    ),
    "glocke_bremen": re.compile(
        r"glocke\.de/event/[^/?#]+",
        re.I,
    ),
    "festspielhaus_baden_baden": re.compile(
        r"festspielhaus\.de/veranstaltungen/[^/?#]+",
        re.I,
    ),
    "liederhalle_stuttgart": re.compile(
        r"liederhalle\.de/.+event[^/?#]+",
        re.I,
    ),
    "isarphilharmonie_muenchen": re.compile(
        r"mphil\.de/.+/\d{4,}",
        re.I,
    ),
}


def _normalize_link(link) -> str:
    if isinstance(link, str):
        return link.strip()
    if hasattr(link, "url"):
        return (link.url or "").strip()
    if isinstance(link, dict):
        return (link.get("url") or link.get("href") or "").strip()
    return ""


def _same_domain(url: str, base_url: str) -> bool:
    domain = urlparse(base_url).netloc
    parsed = urlparse(url)
    return not parsed.netloc or parsed.netloc == domain


def _generic_strict_detail(url: str) -> bool:
    """Fallback when no venue-specific pattern is defined."""
    path = urlparse(url).path
    if _LISTING_PATH_RE.search(path) or _NOISE_PATH_RE.search(path):
        return False
    if _NUMERIC_DETAIL_TAIL_RE.search(path):
        return bool(_EVENT_PATH_LOOSE_RE.search(path))
    # At least two path segments after konzert/veranstaltung keyword.
    m = re.search(
        r"/(?:veranstaltung|veranstaltungen|konzert|konzerte|event|events)"
        r"/([^/?#]+/[^/?#]+)",
        path,
        re.I,
    )
    return bool(m)


def is_loose_event_url(url: str, base_url: str) -> bool:
    if not url or not _same_domain(url, base_url):
        return False
    if _EXCLUDE_RE.search(url):
        return False
    if _LISTING_PATH_RE.search(urlparse(url).path):
        return False
    return bool(_EVENT_PATH_LOOSE_RE.search(url))


def is_strict_event_url(url: str, base_url: str, venue_slug: str | None = None) -> bool:
    if not url or not _same_domain(url, base_url):
        return False
    if _EXCLUDE_RE.search(url):
        return False
    path = urlparse(url).path
    if _LISTING_PATH_RE.search(path):
        return False
    if _NOISE_PATH_RE.search(path):
        return False
    pattern = VENUE_STRICT_PATTERNS.get(venue_slug or "")
    if pattern:
        # Venue has an explicit URL pattern — trust it directly without requiring
        # the generic loose keyword (/veranstaltung/, /konzert/ etc.) in the path.
        # Alte Oper uses /de/programm/<slug>/<id> which has no such keyword.
        return bool(pattern.search(url))
    # No venue-specific pattern: fall back to loose keyword requirement + generic shape
    if not _EVENT_PATH_LOOSE_RE.search(url):
        return False
    return _generic_strict_detail(url)


def filter_event_urls(
    links,
    base_url: str,
    *,
    venue_slug: str | None = None,
    strict: bool = False,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    predicate = (
        (lambda u: is_strict_event_url(u, base_url, venue_slug))
        if strict
        else (lambda u: is_loose_event_url(u, base_url))
    )
    for link in links or []:
        url = _normalize_link(link)
        if not url or url in seen:
            continue
        if not predicate(url):
            continue
        seen.add(url)
        out.append(url)
    return out
