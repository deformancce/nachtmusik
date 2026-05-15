"""
Berliner Philharmonie scraper.

Loads the Berliner Philharmoniker concert calendar (berliner-philharmoniker.de),
expands the listing as far as possible, and extracts the concert program +
artists from each event's detail page via Claude.

The result is written to backend/berliner_philharmonie_events.json.

Note: the Berliner Philharmoniker (orchestra) and the Berliner Philharmonie
(building) share the same operator (Stiftung Berliner Philharmoniker), so
their concert listings live on the same site.

Usage:
  python3 scrape/berliner_philharmonie_scraper.py                # full run
  python3 scrape/berliner_philharmonie_scraper.py --no-detail    # listing only
  python3 scrape/berliner_philharmonie_scraper.py --clicks 80    # raise cap
  python3 scrape/berliner_philharmonie_scraper.py --single-cat /de/konzerte/  # subprocess mode
"""
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

# Confirmed URL: /konzerte/kalender/ is the SPA-based concert calendar.
# The "#/" hash fragment is client-side routing — browsers don't send it to
# the server, but Playwright still navigates to it correctly. The page is a
# JavaScript SPA, so the events appear in the DOM only AFTER the JS bundle
# loads and fetches them. fetch_with_playwright_session() handles this via
# its post-goto wait + page.content() approach.
BASE = "https://www.berliner-philharmoniker.de"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; op.us/0.1)"}
LOAD_MORE_KEYWORDS = (
    # SPA calendar likely uses month navigation, not a "load more" button.
    # Include date-navigation labels too in case they're clickable.
    "Weitere Konzerte",
    "Weitere Veranstaltungen",
    "Weitere Termine",
    "Weitere anzeigen",
    "Weitere laden",
    "Mehr laden",
    "Mehr anzeigen",
    "Show more",
    "Load more",
    "Nächster Monat",
    "Next month",
)
OUTPUT_PATH = Path(__file__).parent.parent / "berliner_philharmonie_events.json"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # cheap + good enough for HTML extraction
MAX_DETAIL_TEXT_CHARS = 40_000  # plain-text content sent to Claude per event

# Single entry point: the SPA-based concert calendar.
# We scroll and click any "load more" / "next month" controls until exhausted.
MAIN_PAGE = "/konzerte/kalender/"


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_with_playwright_session(page, url: str, max_clicks: int = 10,
                                  verbose: bool = True) -> tuple[str, int]:
    """Load a category page in the given Playwright page and click
    'Weitere Veranstaltungen laden' repeatedly until no more appear or
    max_clicks is reached. The caller owns the page lifecycle and the
    browser, so launching 15 separate browsers (which hangs on resource
    pressure) is avoided.

    Returns (rendered HTML, click count)."""

    def _count_from_html(h: str) -> int:
        # Count anything that looks like a concert link in the current DOM.
        # On the Berliner Philharmoniker SPA the calendar renders events as
        # links matching /konzert/, /konzerte/<slug>/ etc., not as fixed-class
        # teasers — so use the same predicate as parse_teasers does.
        soup = BeautifulSoup(h, "lxml")
        return sum(
            1 for a in soup.find_all("a", href=True)
            if _looks_like_event_link(a["href"])
        )

    # SPA: wait for networkidle so the JS bundle has loaded and event data
    # has been fetched. Fall back to domcontentloaded + sleep if networkidle
    # doesn't settle within the timeout (some sites have permanent pings).
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception as exc:
        if verbose:
            print(f"    networkidle timeout — falling back to domcontentloaded: {exc}", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(4.0)  # extra cushion for late-rendering SPA content

    html = page.content()  # has a default timeout; never hangs like evaluate()
    initial = _count_from_html(html)
    if verbose:
        print(f"    initial event links: {initial}", flush=True)

    # The Berliner Philharmoniker calendar is a SPA with infinite scroll —
    # no "load more" button to click. We scroll to the bottom, wait for
    # newly-loaded events to render, and repeat until the count stops
    # growing (2 consecutive scrolls with no new events → end of list).
    clicks = 0
    last_count = initial
    stagnant = 0
    for i in range(max_clicks):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        except Exception as exc:
            if verbose:
                print(f"    scroll {i+1}: evaluate raised {exc}", flush=True)
            break
        clicks += 1
        time.sleep(2.0)  # let the SPA fetch + render the next batch

        html = page.content()
        new_count = _count_from_html(html)
        if verbose:
            print(f"    scroll {clicks}: {last_count} → {new_count}", flush=True)

        if new_count <= last_count:
            stagnant += 1
            if stagnant >= 2:
                # Two scrolls with no new events — we've hit the bottom
                if verbose:
                    print(f"    no growth for {stagnant} scrolls — done", flush=True)
                break
        else:
            stagnant = 0
        last_count = new_count

    return html, clicks


# Kept for backward compatibility / standalone calls; opens its own browser.
def fetch_with_playwright(url: str, max_clicks: int = 10,
                          verbose: bool = True) -> tuple[str, int]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(45000)
            try:
                return fetch_with_playwright_session(page, url, max_clicks, verbose)
            finally:
                page.close()
        finally:
            browser.close()


# Verified URL shape for an event detail page on the Berliner Philharmoniker
# site: https://www.berliner-philharmoniker.de/konzerte/kalender/<numeric_id>/
# Anything else under /konzerte/ (e.g. /abos-und-flex-pakete,
# /saisonhighlights, /ticketinfo) is a navigation/info page and must be
# rejected. External links to digitalconcerthall.com are excluded entirely
# (those are stream tickets, not events at the venue).
_EVENT_URL_RE = re.compile(
    r"^(?:https?://(?:www\.)?berliner-philharmoniker\.de)?/konzerte/kalender/\d+/?$",
    re.IGNORECASE,
)
_EVENT_ID_RE = re.compile(r"/konzerte/kalender/(\d+)/?$")


def _looks_like_event_link(href: str) -> bool:
    if not href or ".ics" in href:
        return False
    # Strip query/fragment before matching the path shape
    clean = href.split("#")[0].split("?")[0]
    return bool(_EVENT_URL_RE.match(clean))


def _nearest_event_container(node, max_depth: int = 6):
    """Walk up from a link to find the enclosing card/teaser/article element."""
    el = node
    for _ in range(max_depth):
        el = el.parent
        if el is None or el.name == "body":
            return node.parent
        cls = " ".join(el.get("class", [])).lower()
        if any(k in cls for k in ("teaser", "card", "event", "concert", "konzert", "item")):
            return el
        if el.name == "article":
            return el
    return node.parent


# Common composer/work line shape: name (with comma or not) followed by work.
# We use it as a coarse filter so we don't pick up boilerplate text like
# "Programmänderungen vorbehalten" or "Pause".
_BOILERPLATE = re.compile(
    r"^(pause|ende|beginn|einlass|programm$|programmänderung|hinweis|"
    r"besetzung|preise|tickets|programmheft|aufführungsdauer)\b",
    re.IGNORECASE,
)


def _looks_like_work(text: str) -> bool:
    if not text or len(text) < 8 or len(text) > 400:
        return False
    if _BOILERPLATE.match(text):
        return False
    # Strong signals that this is a concert work line
    if (":" in text
            or "op." in text.lower()
            or "Nr." in text
            or re.search(r"\b\d+\.\s+[A-ZÄÖÜ]", text)  # "1. Klavierkonzert"
            or any(tok in text for tok in ("BWV", "KV", "K.", "Hob.", "D.", "WAB"))):
        return True
    # Fallback: two consecutive words starting with capital letters (looks like a name).
    return bool(re.search(r"\b[A-ZÄÖÜ][a-zäöüß]+ [A-ZÄÖÜ][a-zäöüß]+", text))


def _extract_program_nodes(soup: BeautifulSoup) -> list[str]:
    """Try several selector strategies; return the first non-empty list of works."""
    candidates = [
        # Common TYPO3 / classical-venue class patterns
        '[class*="program"] li',
        '[class*="program"] p',
        '[class*="programm"] li',
        '[class*="programm"] p',
        '[class*="werke"] li',
        '[class*="werke"] p',
        # Generic article body lines (worst case)
        ".event-detail p",
        ".event-detail li",
    ]
    for selector in candidates:
        nodes = soup.select(selector)
        works = []
        for n in nodes:
            text = " ".join(n.get_text(" ", strip=True).split())
            if _looks_like_work(text):
                works.append(text)
        if works:
            # Deduplicate while keeping order
            seen = set()
            ordered = []
            for w in works:
                if w not in seen:
                    seen.add(w)
                    ordered.append(w)
            return ordered
    return []


def _extract_program_after_heading(soup: BeautifulSoup) -> list[str]:
    """Find a 'Programm' heading and grab work lines that follow it."""
    heading = soup.find(
        ["h1", "h2", "h3", "h4"],
        string=lambda s: s and "programm" in s.strip().lower() and len(s.strip()) < 20,
    )
    if not heading:
        return []
    works: list[str] = []
    for sib in heading.find_all_next():
        if sib.name in ("h1", "h2", "h3") and sib is not heading:
            break
        if sib.name in ("p", "li", "div"):
            text = " ".join(sib.get_text(" ", strip=True).split())
            if _looks_like_work(text) and text not in works:
                works.append(text)
        if len(works) >= 30:
            break
    return works


_DEBUG_DUMP_PATH = Path(__file__).parent.parent / "_debug_detail_sample.html"
_DEBUG_DUMPED = False


def _dump_debug(content: str, label: str) -> None:
    """Write a one-time diagnostic file to backend/_debug_detail_sample.html."""
    global _DEBUG_DUMPED
    if _DEBUG_DUMPED:
        return
    try:
        with open(_DEBUG_DUMP_PATH, "w", encoding="utf-8") as f:
            f.write(f"<!-- DIAGNOSTIC DUMP: {label} -->\n")
            f.write(content)
        _DEBUG_DUMPED = True
        print(f"  [debug] wrote {_DEBUG_DUMP_PATH.name} ({label}, {len(content)} chars)")
    except Exception as exc:
        print(f"  [debug] dump failed: {exc}")


_BAD_TITLES = {"mehr lesen", "streamen", "tickets", "ticket", "details"}


def _is_bad_title(s) -> bool:
    return isinstance(s, str) and s.strip().lower() in _BAD_TITLES


def _looks_mojibaked(text: str) -> bool:
    """Detect leftover UTF-8-as-Latin-1 mojibake from earlier broken runs.
    These sequences ('Â»', 'Ã¼', 'Ã¶', 'Ã¤', 'Ã¢', 'Å¾') don't appear in real
    German concert metadata, so their presence means we should drop the
    cached entry and re-fetch with the encoding fix in place."""
    if not isinstance(text, str):
        return False
    return any(seq in text for seq in ("Ã¼", "Ã¶", "Ã¤", "Ã©", "Ã¨", "Ã ", "Â»", "Â«", "Â§", "Å¾", "Å¡", "Ã "))


def _entry_has_mojibake(ev: dict) -> bool:
    """Walk the relevant string-bearing fields of an event and flag if any
    value still carries mojibake."""
    for v in (ev.get("program") or []):
        if _looks_mojibaked(v):
            return True
    for v in (ev.get("artists") or []):
        if _looks_mojibaked(v):
            return True
    for f in ("title", "subtitle", "description", "hall", "venue", "city",
              "duration", "prices", "prices_reduced", "organizer", "intro",
              "abo", "language"):
        if _looks_mojibaked(ev.get(f, "") or ""):
            return True
    return False


def _load_program_cache() -> dict:
    """Read previously-scraped events from the output JSON and group them
    by detail URL. A single detail URL can map to multiple events (the same
    concert programme played on several nights), so the cache is
    {url: [event, event, ...]}.

    Only events with a non-empty program are cached — empty ones will be
    re-fetched, giving them a chance to succeed on a later run."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    cache: dict = {}
    dropped_mojibake = 0
    for ev in data.get("events", []):
        url = ev.get("url")
        prog = ev.get("program")
        if not url or not prog:
            continue
        # Old runs hit a charset bug and saved Â»/Ã¼ etc. Drop those entries
        # entirely — the cache should not propagate broken text. Next run
        # will re-fetch with proper encoding handling.
        if _entry_has_mojibake(ev):
            dropped_mojibake += 1
            continue
        # Wipe known-bogus titles so the next pass re-extracts them.
        if _is_bad_title(ev.get("title")):
            ev["title"] = ""
        cache.setdefault(url, []).append(ev)
    if dropped_mojibake:
        print(f"  (cache: dropped {dropped_mojibake} entries with mojibake — will re-fetch)",
              flush=True)
    return cache


_CLAUDE_CLIENT = None


def _get_claude_client():
    """Lazy-init Anthropic client. Returns None if no key set or lib missing."""
    global _CLAUDE_CLIENT
    if _CLAUDE_CLIENT is not None:
        return _CLAUDE_CLIENT
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    # max_retries=5 absorbs transient 429/5xx/529 (overloaded) errors with
    # exponential backoff. SDK default is 2; on busy days we routinely see
    # OverloadedError 529 chains that need more retries to ride out.
    _CLAUDE_CLIENT = anthropic.Anthropic(api_key=api_key, max_retries=5)
    return _CLAUDE_CLIENT


def _diagnose_claude_env() -> None:
    """Print a one-line summary of whether Claude can be used."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Claude diagnostic: ANTHROPIC_API_KEY is NOT set in env", flush=True)
        return
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "(too short)"
    print(f"Claude diagnostic: ANTHROPIC_API_KEY present ({masked}, {len(key)} chars)", flush=True)
    try:
        import anthropic
        print(f"Claude diagnostic: anthropic SDK version {anthropic.__version__} importable", flush=True)
    except ImportError as exc:
        print(f"Claude diagnostic: anthropic SDK NOT importable ({exc})", flush=True)
        return
    # Probe the API with a 1-token request to verify key works
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Say OK."}],
        )
        out = msg.content[0].text if msg.content else "(no content)"
        print(f"Claude diagnostic: probe call OK, model replied {out!r}", flush=True)
    except Exception as exc:
        print(f"Claude diagnostic: probe call FAILED — {type(exc).__name__}: {str(exc)[:200]}", flush=True)


_TRIM_BOUNDARIES = (
    "Preise:",
    "Preise :",
    "Veranstalter:",
    "Veranstalter :",
    "Ermäßigung für Berechtigte",
    "Tickets kaufen",
    "Programmänderung",
    "Aufführungsdauer:",
)


def _trim_after_boilerplate(text: str) -> str:
    """Cut the page text at the first occurrence of typical footer boilerplate
    (prices, organizer, programme-change note). Programs always appear above
    these markers on most German concert pages, so trimming saves ~25-40% of tokens
    without losing extraction quality."""
    earliest = len(text)
    for marker in _TRIM_BOUNDARIES:
        idx = text.find(marker)
        if idx >= 0 and idx < earliest:
            earliest = idx
    return text[:earliest].rstrip()


def _select_event_text(soup: BeautifulSoup) -> "str":
    """Return the plain-text content of the event-main container.

    Tries event-detail / article / main containers in turn, then falls back to
    the cleaned <body>. HTML tags are dropped — Claude only needs the words,
    not the markup, and stripping tags halves the token cost.
    """
    for selector in (
        '[class*="event-detail"]',
        '[class*="event__detail"]',
        '[class*="event-content"]',
        "article",
        "main",
        '[role="main"]',
        "#content",
        '[class*="content-main"]',
    ):
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 200:
            text = node.get_text("\n", strip=True)
            return _trim_after_boilerplate(text)[:MAX_DETAIL_TEXT_CHARS]

    body = soup.find("body") or soup
    return _trim_after_boilerplate(body.get_text("\n", strip=True))[:MAX_DETAIL_TEXT_CHARS]


# Instruction block sent as a cached system message. Stays identical across
# all 366 events in a run so Anthropic's prompt cache (5-min TTL) charges us
# at the 10% cache-read rate after the first call.
_CLAUDE_SYSTEM = """You extract structured concert data from a Berliner \
Philharmonie event detail page and return it as a single JSON object.

CRITICAL: only extract works/artists/info that belong to THIS specific \
concert. Detail pages can carry cross-promotion or related-concert blocks \
referring to OTHER events — ignore those. If unsure whether a work belongs \
to this concert or to a recommendation block, prefer to omit it.

Required keys:

"series" (string): the category/format label, e.g. "Lunchkonzert", \
"Kammermusik", "Oper", "Konzert", "Orgel". Empty string if the page does not \
label it.

"title" (string): the concert's headline. Prefer the orchestra/ensemble \
and conductor in the form "<Ensemble> · <Conductor>" — e.g. \
"Berliner Philharmoniker · Klaus Mäkelä" or \
"Quatuor Danel · Marc-André Hamelin". For operas use the work title \
("CARMEN", "LA TRAVIATA"). Empty string only if absolutely nothing usable.

"subtitle" (string): supplementary line. Soloist(s) with role and/or works \
in short, e.g. "Yulianna Avdeeva (Klavier) · Werke von Rachmaninoff, \
Schostakowitsch" or "Mahlers Dritte". Empty string allowed.

"dates" (array): one entry per performance date listed on the page. The \
Berliner site shows recurring concerts (same program, multiple nights) on \
one detail page — list ALL of them. Each entry: \
{"date": "YYYY-MM-DD", "time": "HH:MM"}. \
Example for a 3-night run: \
[{"date":"2026-05-14","time":"20:00"},{"date":"2026-05-15","time":"20:00"},{"date":"2026-05-16","time":"19:00"}]. \
Use [] only if no date is shown.

"venue" (string): the venue / building name, e.g. "Berliner Philharmonie", \
"Waldbühne Berlin". Default to "Berliner Philharmonie" if the page is \
clearly inside the building but does not name the venue explicitly.

"hall" (string): the hall / room within the venue, e.g. "Großer Saal", \
"Kammermusiksaal", "Foyer Großer Saal". Empty string if not stated.

"city" (string): the city, usually "Berlin". Empty string if not stated.

"works" (array of strings): each work formatted "Composer: Work Title \
(opus/catalog number)", e.g. "Ludwig van Beethoven: Symphonie Nr. 9 d-Moll \
op. 125". Operas, oratorios and ballets are one entry. List works in \
performance order. Skip "Pause", "Einlass", "Ende ca.". Use [] only if no \
composer-work information is present.

"artists" (array of strings): each performer with role, e.g. \
"Kirill Petrenko (Dirigent)", "Yuja Wang (Klavier)", "Berliner Philharmoniker". \
Skip ticket-info lines and venue names. Use [] when none are listed.

"description" (string): the longer "Info" or "Hintergrund" marketing text \
describing the concert (max ~800 chars; trim to the most informative \
sentences if longer). Empty string when no description is present.

"duration" (string): the total duration as printed, e.g. "ca. 2 Stunden \
(inkl. 20 Minuten Pause)" or "ca. 1 Stunde 40 Minuten". Empty if not stated.

"prices" (string): the price range as printed, e.g. "39 bis 111 €" or \
"24/20 EUR". Empty if not stated.

"prices_reduced" (string): reduced prices when separately listed, e.g. \
"49,29/42,59/29,89 EUR" or "Flexpreise: 26/22 EUR". Empty if none.

"organizer" (string): "Veranstalter: …" line, e.g. "Oper Leipzig", \
"Berliner Philharmoniker" — just the name, no "Veranstalter:" prefix. Empty \
when not stated.

"has_stream" (boolean): true when the page mentions a streaming option \
(e.g. "Streamen", "Digital Concert Hall", "stream verfügbar"). False \
otherwise.

"intro" (string): pre-concert talk info if listed, e.g. \
"Konzerteinführung 19:15 Uhr mit Meike Pfister". Empty when not present.

"abo" (string): subscription label like "Abo G: Konzerte mit den Berliner \
Philharmonikern" if present, else empty.

"language" (string): for operas, the original language and surtitles, e.g. \
"italienisch mit deutschen Übertiteln". Empty when not applicable.

Respond with ONLY the JSON object. No prose, no markdown fences. Use empty \
string ("") or empty array ([]) when a field is genuinely missing — do not \
hallucinate values."""


_EMPTY_CLAUDE_RESULT = {
    "series": "", "title": "", "subtitle": "",
    "dates": [],
    "venue": "", "hall": "", "city": "",
    "works": [], "artists": [],
    "description": "", "duration": "",
    "prices": "", "prices_reduced": "",
    "organizer": "", "has_stream": False,
    "intro": "", "abo": "", "language": "",
}


def extract_program_with_claude(html: str, event: dict, verbose: bool = False) -> "dict":
    """Ask Claude (Haiku) to read a detail page and return all the structured
    fields it can pull from it: series, title, subtitle, dates[], venue,
    hall, city, works[], artists[], description, duration, prices,
    prices_reduced, organizer, has_stream, intro, abo, language.

    Returns a dict with those keys. Missing fields come back as "" / [] /
    False. Returns the empty-shaped dict on a hard failure."""
    empty = dict(_EMPTY_CLAUDE_RESULT)
    empty["dates"] = []
    empty["works"] = []
    empty["artists"] = []
    client = _get_claude_client()
    if client is None:
        return empty

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "header", "footer", "nav"]):
        tag.decompose()

    page_text = _select_event_text(soup)
    if verbose:
        print(f"    [claude] sending {len(page_text)} chars (text) to Claude", flush=True)

    user_msg = f"Event page text:\n{page_text}"
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,  # bumped — description + more fields take more output
            system=[{
                "type": "text",
                "text": _CLAUDE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        data = json.loads(text)
        if isinstance(data, dict):
            # Normalize dates: list of {date,time} dicts. Tolerate the older
            # top-level date+time fields from previous prompt versions.
            raw_dates = data.get("dates") or []
            dates = []
            for d in raw_dates:
                if isinstance(d, dict) and d.get("date"):
                    dates.append({
                        "date": str(d.get("date") or "").strip(),
                        "time": str(d.get("time") or "").strip(),
                    })
            if not dates and (data.get("date") or data.get("time")):
                dates = [{
                    "date": str(data.get("date") or "").strip(),
                    "time": str(data.get("time") or "").strip(),
                }]
            return {
                "series":         str(data.get("series") or "").strip(),
                "title":          str(data.get("title") or "").strip(),
                "subtitle":       str(data.get("subtitle") or "").strip(),
                "dates":          dates,
                "venue":          str(data.get("venue") or "").strip(),
                "hall":           str(data.get("hall") or data.get("location") or "").strip(),
                "city":           str(data.get("city") or "").strip(),
                "works":          [str(w).strip() for w in (data.get("works") or []) if w],
                "artists":        [str(a).strip() for a in (data.get("artists") or []) if a],
                "description":    str(data.get("description") or "").strip(),
                "duration":       str(data.get("duration") or "").strip(),
                "prices":         str(data.get("prices") or "").strip(),
                "prices_reduced": str(data.get("prices_reduced") or "").strip(),
                "organizer":      str(data.get("organizer") or "").strip(),
                "has_stream":     bool(data.get("has_stream")),
                "intro":          str(data.get("intro") or "").strip(),
                "abo":            str(data.get("abo") or "").strip(),
                "language":       str(data.get("language") or "").strip(),
            }
        # Older bare-list shape — tolerate it for any cached responses
        if isinstance(data, list):
            return {**empty, "works": [str(p).strip() for p in data if p]}
    except Exception as exc:
        print(f"  [claude] error for {event.get('id')}: {type(exc).__name__}: {str(exc)[:80]}")
    return empty


def fetch_program_from_detail(url: str, verbose: bool = False) -> list[str]:
    """Fetch a single event's detail page and extract its program (works performed).

    On the very first failure (network error or empty extraction), the raw
    response is dumped to backend/_debug_detail_sample.html so we can refine
    the parser. With verbose=True, prints diagnostics for each attempt.
    """
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as exc:
        if verbose:
            print(f"  [detail] {url} → fetch raised {type(exc).__name__}: {exc}")
        _dump_debug(f"FETCH ERROR for {url}: {type(exc).__name__}: {exc}", "fetch-exception")
        return []

    if r.status_code != 200:
        if verbose:
            print(f"  [detail] {url} → HTTP {r.status_code}")
        _dump_debug(
            f"HTTP {r.status_code} for {url}\n\nResponse body:\n{r.text[:5000]}",
            f"http-{r.status_code}",
        )
        return []

    html = r.text
    soup = BeautifulSoup(html, "lxml")
    program = _extract_program_nodes(soup) or _extract_program_after_heading(soup)

    if verbose:
        print(f"  [detail] {url} → {len(html)} chars, {len(program)} works")

    if not program:
        # Strip noise so the dump stays under ~100KB and is readable
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        body = soup.find("body") or soup
        _dump_debug(
            f"<!-- source: {url} -->\n<!-- size: {len(html)} chars -->\n{body}",
            f"empty-extraction ({url})",
        )
    return program


def _save_checkpoint(events: list[dict], path: Path) -> None:
    payload = {
        "total_events": len(events),
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "events": events,
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


_RICH_FIELDS = (
    "series", "title", "subtitle",
    "venue", "hall", "city",
    "description", "duration",
    "prices", "prices_reduced",
    "organizer", "has_stream",
    "intro", "abo", "language",
)


def _apply_claude_data(event: dict, c: dict) -> None:
    """Copy Claude-extracted fields onto an event, only filling empties so
    re-runs don't clobber existing data. has_stream is a bool — always copy
    when Claude says true (overrides default false)."""
    if c.get("works") and not event.get("program"):
        event["program"] = c["works"]
    if c.get("artists") and not event.get("artists"):
        event["artists"] = c["artists"]
    for field in _RICH_FIELDS:
        val = c.get(field)
        if field == "has_stream":
            if val and not event.get("has_stream"):
                event["has_stream"] = True
            continue
        if isinstance(val, str) and val.strip() and not event.get(field):
            event[field] = val.strip()


def _merge_expansion(events: list[dict], expansion: dict) -> list[dict]:
    """Walk the events list; whenever a URL has been claimed via Claude
    (i.e. exists in `expansion`), substitute the original entry with the
    list of date-expanded events from the expansion table. Stubs whose URL
    is not in `expansion` are kept as-is (they were already enriched via
    cache, or didn't get fetched yet)."""
    out: list[dict] = []
    emitted: set = set()
    for e in events:
        url = e.get("url")
        if url and url in expansion:
            if url not in emitted:
                out.extend(expansion[url])
                emitted.add(url)
            # else: this is a duplicate stub for the same URL — drop it,
            # the expansion above already covers all dates.
        else:
            out.append(e)
    return out


def _build_event_per_date(stub: dict, c: dict) -> list[dict]:
    """Take a stub event + Claude's data (which may contain N dates) and
    return one event per date. Each event has id = "{base_id}-{date}".

    When dates are empty/duplicated, an index suffix is added so two
    sibling events never share an id — the events list dedupes by id in
    the backend and would otherwise drop one silently.
    """
    dates = c.get("dates") or []
    if not dates:
        dates = [{"date": "", "time": ""}]
    base_id = (stub.get("id") or "").split("-", 1)[0] or stub.get("id", "")
    out: list[dict] = []
    seen_ids: set = set()
    for i, d in enumerate(dates):
        clone = dict(stub)
        _apply_claude_data(clone, c)
        date_str = (d.get("date") or "").strip()
        time_str = (d.get("time") or "").strip()
        clone["date"] = date_str or clone.get("date", "")
        clone["time"] = time_str or clone.get("time", "")
        # Prefer date as the disambiguator. When date is missing or
        # collides with a sibling, fall back to an index suffix so each
        # event keeps a unique id.
        eid = f"{base_id}-{date_str}" if date_str else f"{base_id}-{i}"
        while eid in seen_ids:
            eid += "_"
        seen_ids.add(eid)
        clone["id"] = eid
        out.append(clone)
    return out


def enrich_with_detail_programs(
    events: list[dict],
    checkpoint_path: "Path | None" = None,
    checkpoint_every: int = 50,
) -> None:
    """Populate every event from its detail page, going through:
       1. cache replay (cache is keyed by URL — a single URL can yield N
          events when a programme plays on multiple nights),
       2. Claude (Haiku) extraction with multi-date expansion,
       3. static CSS-selector fallback for works only.

    The `events` list is mutated in place: entries can be replaced (via
    cache hit) or expanded (Claude returns multiple dates).
    """
    cache_by_url = _load_program_cache()
    if cache_by_url:
        total = sum(len(v) for v in cache_by_url.values())
        print(f"\nProgram cache: {total} entries across {len(cache_by_url)} URLs",
              flush=True)

    have_claude = _get_claude_client() is not None
    print(f"Claude API: {'enabled (Haiku)' if have_claude else 'disabled (no key)'}",
          flush=True)

    # Pass 1 — replay cache. A stub-from-listing matching a cached URL is
    # replaced wholesale by the cached events (which may be 1 or N).
    replaced: list[dict] = []
    cache_hits = 0
    for stub in events:
        cached = cache_by_url.get(stub.get("url")) or []
        if cached:
            replaced.extend(cached)
            cache_hits += len(cached)
        else:
            replaced.append(stub)
    events.clear()
    events.extend(replaced)
    if cache_hits:
        print(f"Reused {cache_hits} entries from previous run", flush=True)

    # Pass 2 — figure out what's still missing. A "complete" event has at
    # least program + title + date populated.
    def _needs_fetch(e: dict) -> bool:
        if not e.get("url"):
            return False
        return not (e.get("program") and e.get("title") and e.get("date"))

    # Dedupe TODO by URL — a single Claude call covers all dates from a URL.
    todo_by_url: dict = {}
    for e in events:
        if _needs_fetch(e):
            todo_by_url.setdefault(e["url"], e)
    todo = list(todo_by_url.values())

    if not todo:
        print("\nAll events already fully populated — nothing to fetch.", flush=True)
        return

    print(f"\nFetching {len(todo)} detail pages for full extraction...", flush=True)
    enriched_claude = 0
    enriched_static = 0
    fetch_errors = 0
    fetch_non_200 = 0
    claude_errors = 0
    claude_empty = 0
    # We'll rebuild the events list with multi-date expansion. Stubs we
    # didn't fetch (because they were already complete from cache) pass
    # through unchanged. Stubs we did fetch get replaced by one or more
    # date-specific events.
    expansion: dict = {}  # url → list[event]

    for i, stub in enumerate(todo, 1):
        verbose = i <= 5
        url = stub["url"]
        eid = stub.get("id", "?")

        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            # A1 fix: requests defaults to ISO-8859-1 when the HTTP header
            # lacks an explicit charset, even though the body's <meta>
            # declares utf-8. Force chardet's detection so .text decodes
            # cleanly and we don't end up with Â»…Â« mojibake in the JSON.
            r.encoding = r.apparent_encoding or "utf-8"
        except Exception as exc:
            print(f"  [detail {eid}] FETCH RAISED {type(exc).__name__}: {str(exc)[:120]}")
            fetch_errors += 1
            _dump_debug(
                f"FETCH RAISED for {url}: {type(exc).__name__}: {exc}",
                f"fetch-exception ({url})",
            )
            time.sleep(0.3)
            continue

        if r.status_code != 200:
            print(f"  [detail {eid}] HTTP {r.status_code} for {url}")
            fetch_non_200 += 1
            _dump_debug(
                f"HTTP {r.status_code} for {url}\n\nResponse body (first 5KB):\n{r.text[:5000]}",
                f"http-{r.status_code} ({url})",
            )
            time.sleep(0.3)
            continue

        if verbose:
            print(f"  [detail {eid}] HTTP 200, {len(r.text)} chars")

        claude_data: dict = {}
        if have_claude:
            try:
                claude_data = extract_program_with_claude(
                    r.text, stub, verbose=verbose
                )
            except Exception as exc:
                print(f"  [claude {eid}] EXCEPTION {type(exc).__name__}: {str(exc)[:120]}")
                claude_errors += 1

        works = claude_data.get("works") or []
        dates = claude_data.get("dates") or []
        if verbose:
            preview = works[0][:70] if works else "(empty)"
            print(f"  [claude {eid}] {len(works)}w / {len(claude_data.get('artists') or [])}a / "
                  f"{len(dates)}dates · title={claude_data.get('title','')[:40]!r}: {preview}",
                  flush=True)
        if works:
            enriched_claude += 1
        else:
            claude_empty += 1
            # Static fallback only when Claude found nothing
            soup = BeautifulSoup(r.text, "lxml")
            fallback = _extract_program_nodes(soup) or _extract_program_after_heading(soup)
            if fallback:
                claude_data["works"] = fallback
                works = fallback
                enriched_static += 1

        # Build one event per date (the date may be empty if Claude found
        # none — we still produce a single placeholder so the listing entry
        # isn't lost).
        expanded = _build_event_per_date(stub, claude_data)
        expansion[url] = expanded

        if not works and not _DEBUG_DUMPED:
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                tag.decompose()
            body = soup.find("body") or soup
            _dump_debug(
                f"<!-- source: {url} -->\n<!-- size: {len(r.text)} chars -->\n{body}",
                f"empty-extraction ({url})",
            )

        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} processed | claude:{enriched_claude} static:{enriched_static} "
                  f"fetch_err:{fetch_errors} non200:{fetch_non_200} claude_err:{claude_errors} claude_empty:{claude_empty}",
                  flush=True)

        if checkpoint_path and i % checkpoint_every == 0:
            # Apply all expansions so far so the checkpoint contains the
            # latest enriched + date-expanded events, not the stubs.
            merged = _merge_expansion(events, expansion)
            _save_checkpoint(merged, checkpoint_path)
            print(f"  [checkpoint] saved {checkpoint_path.name} after {i} events ({len(merged)} total)",
                  flush=True)

        time.sleep(0.3)

    # Final apply: replace processed stubs by their expanded forms.
    merged_final = _merge_expansion(events, expansion)
    events.clear()
    events.extend(merged_final)

    print(
        f"\nDetail pass summary:\n"
        f"  programs found: {enriched_claude} via Claude + {enriched_static} via selectors\n"
        f"  fetch failures: {fetch_errors} exceptions, {fetch_non_200} non-200 responses\n"
        f"  claude calls:   {claude_errors} errors, {claude_empty} returned empty list\n"
        f"  total processed: {len(todo)}",
        flush=True,
    )


def parse_teasers(html: str, category: str) -> list[dict]:
    """Listing-page parser: collect event detail URLs and stable IDs only.

    Title/date/time/location come from Claude on the detail page where they
    are clearly labelled. Listing-side DOM parsing is brittle on the SPA
    (titles render as styled divs, dates as plain text), so we don't even
    try here — keeping this function venue-portable.
    """
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []
    seen: set[str] = set()
    source_url = BASE + (category or MAIN_PAGE)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _looks_like_event_link(href):
            continue
        # Normalize: ensure exactly one leading slash before BASE, strip
        # query/fragment, and drop trailing slash so dedupe is consistent.
        if href.startswith("http"):
            url = href
        else:
            url = BASE + "/" + href.lstrip("/")
        url = url.split("#")[0].split("?")[0].rstrip("/")
        if url in seen:
            continue
        seen.add(url)

        m = _EVENT_ID_RE.search(url)
        if not m:
            continue
        event_id = m.group(1)

        events.append({
            "id": event_id,
            "date": "",          # Claude extracts from detail
            "time": "",          # Claude extracts from detail
            "title": "",         # Claude extracts from detail
            "series": "",        # Claude extracts from detail
            "subtitle": "",      # Claude extracts from detail
            "venue": "Berliner Philharmonie",  # default, overridable by Claude
            "hall": "",          # Claude extracts from detail
            "city": "Berlin",    # default, overridable by Claude
            "program": [],
            "artists": [],
            "description": "",
            "duration": "",
            "prices": "",
            "prices_reduced": "",
            "organizer": "",
            "has_stream": False,
            "intro": "",
            "abo": "",
            "language": "",
            "url": url,
            "category": category.strip("/"),
            "source_url": source_url,
        })
    return events


def _scrape_category_subprocess(cat: str, max_clicks: int,
                                timeout_s: int = 90) -> tuple[str, int]:
    """Scrape one category in a child process so any Playwright hang cannot
    block the parent. subprocess.run(timeout=) sends SIGKILL after timeout_s
    regardless of what the child is doing — the only truly reliable mechanism.

    The child is invoked as: python3 this_file.py --single-cat CAT --clicks N
    It writes HTML to stdout and "CLICKS:N" to stderr.
    Returns (html, clicks) or ("", 0) on timeout / error."""
    this_file = str(Path(__file__).resolve())
    try:
        result = subprocess.run(
            [sys.executable, this_file, "--single-cat", cat,
             "--clicks", str(max_clicks)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        print(f"\n    TIMEOUT after {timeout_s}s", flush=True)
        return "", 0
    except Exception as exc:
        print(f"\n    subprocess ERROR: {exc}", flush=True)
        return "", 0

    clicks = 0
    for line in (result.stderr or "").splitlines():
        if line.startswith("CLICKS:"):
            try:
                clicks = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.strip():
            print(f"    {line.strip()}", flush=True)

    if result.returncode != 0 or not result.stdout:
        return "", 0
    return result.stdout, clicks


def scrape_all(use_playwright: bool = True, max_clicks: int = 50) -> list[dict]:
    """Scrape the Berliner Philharmonie concert calendar, scrolling and
    clicking the 'load more' button until all events are loaded.

    Single page, single subprocess, no per-category hang risk."""
    now = datetime.utcnow().isoformat() + "Z"
    url = BASE + MAIN_PAGE

    print(f"  Scraping main page: {url}", flush=True)
    html = ""
    clicks = 0

    if use_playwright:
        # ~2.3s per click + initial load; 60 clicks ≈ 140s today. 600s leaves
        # headroom for slower days or higher --clicks counts later.
        html, clicks = _scrape_category_subprocess(
            MAIN_PAGE, max_clicks=max_clicks, timeout_s=600
        )

    if not html:
        print("  Playwright failed or disabled — static fetch fallback")
        try:
            html = fetch(url)
        except Exception as exc:
            print(f"  static fetch failed: {exc}")
            return []

    events = parse_teasers(html, "")
    for e in events:
        e["scraped_at"] = now
    tag = f"+{clicks} clicks, " if clicks else ""
    print(f"  → {tag}{len(events)} events loaded", flush=True)
    return events


def _run_single_cat_mode(page_path: str, max_clicks: int) -> None:
    """Entry point for --single-cat subprocess mode.
    Scrapes one page path, writes HTML to stdout, 'CLICKS:N' to stderr."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            page.set_default_timeout(60000)
            html, clicks = fetch_with_playwright_session(
                page, BASE + page_path, max_clicks=max_clicks, verbose=True
            )
            ctx.close()
            browser.close()
        print(f"CLICKS:{clicks}", file=sys.stderr, flush=True)
        sys.stdout.write(html)
        sys.stdout.flush()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Berliner Philharmonie scraper")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Skip Playwright rendering; use plain requests")
    parser.add_argument("--no-detail", action="store_true",
                        help="Skip detail-page program extraction")
    parser.add_argument("--clicks", type=int, default=10,
                        help="Max 'load more' clicks (default 10)")
    parser.add_argument("--single-cat", metavar="PATH",
                        help="Internal: scrape one path, write HTML to stdout")
    args = parser.parse_args()

    # Subprocess mode: scrape exactly one path and exit.
    if args.single_cat:
        _run_single_cat_mode(args.single_cat, args.clicks)
        return

    print(f"Berliner Philharmonie scraper — {MAIN_PAGE}, up to {args.clicks} load-more clicks", flush=True)
    print(f"  Playwright: {'off' if args.no_playwright else 'on (subprocess)'}, "
          f"max clicks: {args.clicks}, "
          f"detail pages: {'off' if args.no_detail else 'on'}", flush=True)
    if not args.no_detail:
        _diagnose_claude_env()
    print("=" * 50, flush=True)

    events = scrape_all(use_playwright=not args.no_playwright,
                       max_clicks=args.clicks)
    events.sort(key=lambda e: e.get("date") or "9999-99-99")
    print(f"\nTotal unique events: {len(events)}", flush=True)

    if not args.no_detail:
        enrich_with_detail_programs(events, checkpoint_path=OUTPUT_PATH, checkpoint_every=50)

    _save_checkpoint(events, OUTPUT_PATH)
    print(f"Saved → {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
