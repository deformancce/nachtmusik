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

# TODO(verify on first run): if the events live on a different host or path,
# adjust BASE and MAIN_PAGE here. Most likely candidates:
#   /de/konzerte/   (the orchestra's own concerts + venue events)
#   /en/concerts/   (English equivalent)
#   /de/spielplan/  (alternative path on some classical sites)
BASE = "https://www.berliner-philharmoniker.de"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; op.us/0.1)"}
LOAD_MORE_KEYWORDS = (
    "Weitere Konzerte",
    "Weitere Veranstaltungen",
    "Weitere Termine",
    "Weitere laden",
    "Mehr laden",
    "Mehr anzeigen",
    "Show more",
    "Load more",
)
OUTPUT_PATH = Path(__file__).parent.parent / "berliner_philharmonie_events.json"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # cheap + good enough for HTML extraction
MAX_DETAIL_TEXT_CHARS = 40_000  # plain-text content sent to Claude per event

# Single entry point: the concert calendar page lists all upcoming events.
# We scroll and click "Weitere Konzerte" / "Mehr anzeigen" until exhausted.
MAIN_PAGE = "/de/konzerte/"


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
        # Count via BS4 on already-fetched HTML — avoids page.evaluate() which
        # can block indefinitely when the page's JS thread is unresponsive.
        return len(BeautifulSoup(h, "lxml").find_all(class_="event-teaser"))

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(1.5)

    html = page.content()  # has a default timeout; never hangs like evaluate()
    initial = _count_from_html(html)
    if verbose:
        print(f"    initial teasers: {initial}")

    # Early exit when the page has nothing to offer — check the already-fetched
    # HTML for a load-more button so we avoid any further evaluate() calls.
    if initial == 0:
        soup_init = BeautifulSoup(html, "lxml")
        has_button = any(
            any(kw in (el.get_text() or "") for kw in LOAD_MORE_KEYWORDS)
            for el in soup_init.find_all(["a", "button"])
        )
        if not has_button:
            if verbose:
                print("    0 teasers, no load-more button — skipping clicks")
            return html, 0

    clicks = 0
    last_count = initial
    for i in range(max_clicks):
        # evaluate() is still used for scrolling + clicking because there is no
        # reliable timeout-aware alternative in sync Playwright for arbitrary
        # button text. This call rarely hangs (14/15 categories work fine); the
        # per-context memory isolation in scrape_all() prevents the memory
        # pressure that caused /tacheles/ to block.
        clicked_text = page.evaluate(
            """
            (keywords) => {
                window.scrollTo(0, document.body.scrollHeight);
                const elements = document.querySelectorAll('a, button');
                for (const el of elements) {
                    const text = (el.textContent || '').trim();
                    if (!text) continue;
                    if (keywords.some(kw => text.includes(kw)) && el.offsetParent !== null) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return text;
                    }
                }
                return null;
            }
            """,
            list(LOAD_MORE_KEYWORDS),
        )
        if not clicked_text:
            if verbose:
                print(f"    no button found after {clicks} clicks")
            break
        clicks += 1
        time.sleep(1.5)
        html = page.content()
        new_count = _count_from_html(html)
        if verbose:
            print(f"    click {clicks}: {last_count} → {new_count}")
        if new_count == last_count:
            # Click happened but no new teasers — the load-more is exhausted
            break
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


# Patterns in the URL path that flag a link as an event detail page rather
# than a navigation/static page. Tune on first run if the Berliner site uses
# different URLs.
_EVENT_URL_PATTERNS = (
    "/konzert/",
    "/konzerte/",
    "/concert/",
    "/concerts/",
    "/event/",
    "/events/",
    "/veranstaltung/",
    "/programm-detail/",
    "/programm/detail/",
)

_EVENT_ID_RE = re.compile(r"(\d{4,})")  # numeric ID anywhere in URL


def _looks_like_event_link(href: str) -> bool:
    if not href or ".ics" in href:
        return False
    h = href.lower()
    if not any(p in h for p in _EVENT_URL_PATTERNS):
        return False
    # Must look like a specific event, not the listing root itself.
    # Accept either a numeric ID or a slug containing a hyphen.
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return bool(_EVENT_ID_RE.search(href) or "-" in tail)


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


def _load_program_cache() -> dict[str, dict]:
    """Read previously-scraped programs + artists from the output JSON so we
    don't have to refetch + re-extract them every run.

    Returns {event_id: {"program": [...], "artists": [...]}}. Only events
    that have a non-empty program are cached — events with an empty program
    will be re-fetched, giving them a chance to succeed (or pick up artists)
    on a later run."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    cache: dict[str, dict] = {}
    for ev in data.get("events", []):
        eid = ev.get("id")
        prog = ev.get("program")
        if eid and prog:
            cache[eid] = {
                "program": prog,
                "artists": ev.get("artists") or [],
            }
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
    _CLAUDE_CLIENT = anthropic.Anthropic(api_key=api_key)
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
_CLAUDE_SYSTEM = """You extract structured concert data from Berliner \
Philharmonie event pages and return it as a JSON object with two keys: \
"works" and "artists".

"works" is an array of strings, each formatted "Composer: Work Title \
(opus/catalog number)" when available, e.g.:
- "Johann Sebastian Bach: Weihnachts-Oratorium BWV 248"
- "Ludwig van Beethoven: Symphonie Nr. 9 d-moll op. 125"
- "Gustav Mahler: Symphonie Nr. 2 c-moll \\"Auferstehung\\""
- "Georges Bizet: Carmen (Oper in vier Akten)"

Operas, oratorios and ballets count as a single work — return a one-element \
list with the composer and work, e.g. ["Georges Bizet: Carmen"]. Look for a \
line like "Georges Bizet — Carmen - Oper in vier Akten" on the page.

For concerts with multiple works (symphony concerts, chamber music, recitals, \
choir concerts, etc.), list each work separately in performance order.

Skip filler like "Pause", "Einlass", "Ende ca.", and lines that are only \
artist/conductor names. Use "works": [] only if you genuinely cannot find any \
composer-work information.

"artists" is an array of strings — each performer or staff member with their \
role, e.g.:
- "Kirill Petrenko (Dirigent / Conductor)"
- "Yuja Wang (Klavier / Piano)"
- "Berliner Philharmoniker"
- "Lindy Hume (Inszenierung / Director)"

Include the conductor, soloists, ensembles, and (for operas) director/staging. \
Roles may appear in German or English on the page — keep whichever the page \
uses. Skip generic ticket-info lines and venue names. Use "artists": [] when \
none are listed.

Respond with ONLY the JSON object. No prose, no markdown fences. Example:
{"works": ["Ludwig van Beethoven: Symphonie Nr. 5 c-Moll op. 67"], "artists": ["Kirill Petrenko (Dirigent)", "Berliner Philharmoniker"]}"""


def extract_program_with_claude(html: str, event: dict, verbose: bool = False) -> "tuple[list[str], list[str]]":
    """Ask Claude (Haiku) to read a detail page and return (works, artists).

    works   — list of 'Composer: Work (opus)' strings
    artists — list of 'Name (Role)' strings (conductor, soloists, ensembles)

    Returns ([], []) on failure or when no data can be extracted."""
    client = _get_claude_client()
    if client is None:
        return [], []

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "header", "footer", "nav"]):
        tag.decompose()

    page_text = _select_event_text(soup)
    if verbose:
        print(f"    [claude] sending {len(page_text)} chars (text) to Claude", flush=True)

    user_msg = (
        f"Event title: {event.get('title', '')}\n"
        f"Event date: {event.get('date', '')}\n\n"
        f"Event page text:\n{page_text}"
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
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
        # Tolerate either the new object schema or the older bare-list shape
        # (in case Claude regresses) — older runs returned just a list.
        if isinstance(data, dict):
            works = [str(w).strip() for w in (data.get("works") or []) if w]
            artists = [str(a).strip() for a in (data.get("artists") or []) if a]
            return works, artists
        if isinstance(data, list):
            return [str(p).strip() for p in data if p], []
    except Exception as exc:
        print(f"  [claude] error for {event.get('id')}: {type(exc).__name__}: {str(exc)[:80]}")
    return [], []


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


def enrich_with_detail_programs(
    events: list[dict],
    checkpoint_path: "Path | None" = None,
    checkpoint_every: int = 50,
) -> None:
    """Populate event['program'] by, in order:
       1. Reusing data from the previous output JSON (cache).
       2. Asking Claude (Haiku) to extract the program from the detail page,
          when ANTHROPIC_API_KEY is set.
       3. Falling back to static CSS-selector parsing.

    If checkpoint_path is given, a partial JSON is written every
    checkpoint_every events so a crash or cancel doesn't lose work.
    """
    cache = _load_program_cache()
    if cache:
        print(f"\nProgram cache: {len(cache)} previously-extracted entries", flush=True)

    have_claude = _get_claude_client() is not None
    print(f"Claude API: {'enabled (Haiku)' if have_claude else 'disabled (no key)'}", flush=True)

    cache_hits = 0
    for ev in events:
        cached = cache.get(ev.get("id"))
        if not cached:
            continue
        if not ev.get("program"):
            ev["program"] = cached["program"]
            cache_hits += 1
        if not ev.get("artists") and cached.get("artists"):
            ev["artists"] = cached["artists"]
    if cache_hits:
        print(f"Reused {cache_hits} programs from previous run", flush=True)

    # Re-fetch any event that's missing EITHER a program OR an artists list —
    # so events from older runs (program-only schema) get topped up on the
    # next pass.
    todo = [
        e for e in events
        if e.get("url") and (not e.get("program") or not e.get("artists"))
    ]
    if not todo:
        print("\nAll events already have program + artists — nothing to fetch.", flush=True)
        return

    print(f"\nFetching {len(todo)} detail pages for program/artist extraction...", flush=True)
    enriched_claude = 0
    enriched_static = 0
    fetch_errors = 0
    fetch_non_200 = 0
    claude_errors = 0
    claude_empty = 0
    for i, event in enumerate(todo, 1):
        verbose = i <= 5  # noisy on the first five, then quiet
        eid = event.get("id", "?")

        try:
            r = requests.get(event["url"], headers=HEADERS, timeout=20)
        except Exception as exc:
            print(f"  [detail {eid}] FETCH RAISED {type(exc).__name__}: {str(exc)[:120]}")
            fetch_errors += 1
            _dump_debug(
                f"FETCH RAISED for {event['url']}: {type(exc).__name__}: {exc}",
                f"fetch-exception ({event['url']})",
            )
            time.sleep(0.3)
            continue

        if r.status_code != 200:
            print(f"  [detail {eid}] HTTP {r.status_code} for {event['url']}")
            fetch_non_200 += 1
            _dump_debug(
                f"HTTP {r.status_code} for {event['url']}\n\nResponse body (first 5KB):\n{r.text[:5000]}",
                f"http-{r.status_code} ({event['url']})",
            )
            time.sleep(0.3)
            continue

        if verbose:
            print(f"  [detail {eid}] HTTP 200, {len(r.text)} chars")

        program: list[str] = []
        artists_from_claude: list[str] = []
        claude_attempted = False
        if have_claude:
            claude_attempted = True
            try:
                program, artists_from_claude = extract_program_with_claude(
                    r.text, event, verbose=verbose
                )
            except Exception as exc:
                print(f"  [claude {eid}] EXCEPTION {type(exc).__name__}: {str(exc)[:120]}")
                claude_errors += 1
            if verbose:
                preview = program[0][:70] if program else "(empty)"
                print(f"  [claude {eid}] returned {len(program)} works, "
                      f"{len(artists_from_claude)} artists: {preview}")
            if not program:
                claude_empty += 1

        if not program:
            soup = BeautifulSoup(r.text, "lxml")
            program = _extract_program_nodes(soup) or _extract_program_after_heading(soup)
            if program:
                enriched_static += 1
        elif claude_attempted:
            enriched_claude += 1

        # Only set program when the event doesn't already have one — we may
        # be re-fetching this page solely to backfill artists.
        if program and not event.get("program"):
            event["program"] = program
        if artists_from_claude and not event.get("artists"):
            event["artists"] = artists_from_claude
        if not event.get("program") and not _DEBUG_DUMPED:
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                tag.decompose()
            body = soup.find("body") or soup
            _dump_debug(
                f"<!-- source: {event['url']} -->\n<!-- size: {len(r.text)} chars -->\n{body}",
                f"empty-extraction ({event['url']})",
            )

        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} processed | claude:{enriched_claude} static:{enriched_static} "
                  f"fetch_err:{fetch_errors} non200:{fetch_non_200} claude_err:{claude_errors} claude_empty:{claude_empty}",
                  flush=True)

        if checkpoint_path and i % checkpoint_every == 0:
            _save_checkpoint(events, checkpoint_path)
            print(f"  [checkpoint] saved {checkpoint_path.name} after {i} events", flush=True)

        time.sleep(0.3)

    print(
        f"\nDetail pass summary:\n"
        f"  programs found: {enriched_claude} via Claude + {enriched_static} via selectors\n"
        f"  fetch failures: {fetch_errors} exceptions, {fetch_non_200} non-200 responses\n"
        f"  claude calls:   {claude_errors} errors, {claude_empty} returned empty list\n"
        f"  total processed: {len(todo)}",
        flush=True,
    )


def parse_teasers(html: str, category: str) -> list[dict]:
    """Generic listing-page parser: find event detail URLs by URL pattern,
    then walk up to the nearest container to grab a date and title.

    Designed to work with minimal venue knowledge — Claude does the heavy
    lifting on each detail page (program, artists, location, etc.).
    """
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []
    seen: set[str] = set()
    source_url = BASE + (category or MAIN_PAGE)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _looks_like_event_link(href):
            continue
        url = href if href.startswith("http") else BASE + href.lstrip("/").rjust(len(href) + 1, "/")
        # Normalize: strip query/fragment for dedupe
        norm = url.split("#")[0].split("?")[0].rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        url = norm

        container = _nearest_event_container(a)

        # Date — prefer <time datetime=...>; fall back to ISO-like strings in text
        date = ""
        if container is not None:
            date_el = container.find("time", attrs={"datetime": True})
            if date_el and date_el.get("datetime"):
                date = date_el["datetime"][:10]
        if not date:
            time_el = a.find("time", attrs={"datetime": True})
            if time_el and time_el.get("datetime"):
                date = time_el["datetime"][:10]

        # Title — prefer h1-h4 in container, else the link's own text
        title = ""
        if container is not None:
            for tag in ("h1", "h2", "h3", "h4"):
                h = container.find(tag)
                if h:
                    title = h.get_text(" ", strip=True)
                    break
        if not title:
            title = a.get_text(" ", strip=True)
        title = title[:300]

        # Stable-ish ID: numeric chunk in the URL, else last path segment
        m = _EVENT_ID_RE.search(url)
        event_id = m.group(1) if m else url.rsplit("/", 1)[-1] or url

        events.append({
            "id": event_id,
            "date": date,
            "time": "",          # Claude extracts from detail
            "title": title,
            "series": "",        # Claude extracts from detail
            "location": "",      # Claude extracts from detail
            "program": [],
            "artists": [],
            "url": url,
            "venue": "Berliner Philharmonie",
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
