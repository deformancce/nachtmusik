"""
Gewandhaus Leipzig scraper.

For each of the 15 concert category pages on gewandhausorchester.de:
  1. Render the page with Playwright and click "Weitere Veranstaltungen laden"
     up to N times to expose lazy-loaded events (falls back to a static fetch
     if Playwright isn't available).
  2. Parse the event teasers from the rendered HTML.
  3. For every event without a listing-derived program, follow its detail URL
     and extract the program (works performed) from the detail page.

The result is written to backend/gewandhaus_events.json.

Usage:
  python3 scrape/gewandhaus_scraper.py                   # full run
  python3 scrape/gewandhaus_scraper.py --no-playwright   # static-only
  python3 scrape/gewandhaus_scraper.py --no-detail       # listing only
  python3 scrape/gewandhaus_scraper.py --clicks 20       # raise load-more cap
"""
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.gewandhausorchester.de"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; op.us/0.1)"}
LOAD_MORE_KEYWORDS = (
    "Weitere Veranstaltungen",
    "Weitere laden",
    "Mehr laden",
    "Mehr anzeigen",
)
OUTPUT_PATH = Path(__file__).parent.parent / "gewandhaus_events.json"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # cheap + good enough for HTML extraction
MAX_DETAIL_HTML_CHARS = 30_000

CATEGORIES = [
    "/grosse-concerte/",
    "/kammermusik/",
    "/klaviermusik/",
    "/orgel/",
    "/choere/",
    "/alte-musik/",
    "/musica-nova/",
    "/salonmusik/",
    "/klassik-airleben/",
    "/impuls/",
    "/in-der-thomaskirche/",
    "/in-der-oper/",
    "/nachklang/",
    "/perspektivwechsel/",
    "/tacheles/",
]


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


_TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})|(\d{1,2})\s*Uhr")


def parse_time(teaser) -> str:
    el = teaser.find(class_=lambda c: c and "event-teaser__time" in " ".join(c if isinstance(c, list) else [c]))
    if not el:
        return ""
    raw = el.get_text(" ", strip=True)  # e.g. "19.30 Uhr" or "17 Uhr"
    m = _TIME_RE.search(raw)
    if not m:
        return raw.replace(" Uhr", "").strip()
    if m.group(1) is not None:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return f"{int(m.group(3)):02d}:00"


def parse_location(teaser) -> str:
    date_info = teaser.find(class_=lambda c: c and "event-teaser__date-info" in " ".join(c if isinstance(c, list) else [c]))
    if not date_info:
        return ""
    for p in date_info.find_all("p"):
        text = p.get_text(" ", strip=True)
        # The location follows the time span — skip lines that are just the time
        if "Uhr" in text:
            # Location is the text after the <br>
            parts = [t.strip() for t in p.get_text("\n", strip=True).split("\n") if t.strip()]
            for part in parts:
                if "Uhr" not in part and len(part) > 2:
                    return part
    return ""


def parse_series(teaser) -> str:
    el = teaser.find(class_=lambda c: c and "text-highlight--color-primary" in " ".join(c if isinstance(c, list) else [c]))
    return el.get_text(strip=True) if el else ""


def parse_title_artists(teaser):
    h2 = teaser.find("h2")
    if not h2:
        return "", []
    title = h2.get_text(" ", strip=True)
    artists: list[str] = []
    for sib in h2.find_next_siblings("p"):
        # Stop at program-related blocks
        parent_classes = " ".join(sib.parent.get("class", []))
        if "details-level-1" in parent_classes or "details-hide" in parent_classes:
            break
        txt = sib.get_text(" ", strip=True)
        if txt and "Werke von" not in txt and txt != title:
            artists.append(txt)
    return title, artists


def parse_program(teaser) -> list[str]:
    level1 = teaser.find(
        class_=lambda c: c and "js-event-teaser-details-level-1" in " ".join(c if isinstance(c, list) else [c])
    )
    if not level1:
        return []
    items = []
    for p in level1.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if txt and txt.lower() not in ("pause", ""):
            items.append(txt)
    return items


def parse_detail_url(teaser) -> str:
    link = teaser.find("a", href=lambda h: h and "/veranstaltung/" in h and ".ics" not in h)
    if link:
        href = link["href"]
        return href if href.startswith("http") else BASE + href
    return ""


def parse_event_id(teaser) -> str:
    eid = teaser.get("id", "")  # e.g. "event-9373"
    return eid.replace("event-", "") if eid else ""


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
        # TYPO3 / Gewandhaus-style class patterns
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


def _load_program_cache() -> dict[str, list[str]]:
    """Read previously-scraped programs from gewandhaus_events.json so we
    don't have to refetch + re-extract them every run."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    cache: dict[str, list[str]] = {}
    for ev in data.get("events", []):
        eid = ev.get("id")
        prog = ev.get("program")
        if eid and prog:
            cache[eid] = prog
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
        print("Claude diagnostic: ANTHROPIC_API_KEY is NOT set in env")
        return
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "(too short)"
    print(f"Claude diagnostic: ANTHROPIC_API_KEY present ({masked}, {len(key)} chars)")
    try:
        import anthropic
        print(f"Claude diagnostic: anthropic SDK version {anthropic.__version__} importable")
    except ImportError as exc:
        print(f"Claude diagnostic: anthropic SDK NOT importable ({exc})")
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
        print(f"Claude diagnostic: probe call OK, model replied {out!r}")
    except Exception as exc:
        print(f"Claude diagnostic: probe call FAILED — {type(exc).__name__}: {str(exc)[:200]}")


def extract_program_with_claude(html: str, event: dict) -> list[str]:
    """Ask Claude (Haiku) to read a detail page and return the program as a
    JSON list of strings like 'Composer: Work (opus)'. Returns [] on failure."""
    client = _get_claude_client()
    if client is None:
        return []

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    body = soup.find("body") or soup
    body_html = str(body)[:MAX_DETAIL_HTML_CHARS]

    prompt = f"""Extract the concert program from this Gewandhaus Leipzig event detail page.

Event title: {event.get('title', '')}
Event date: {event.get('date', '')}

HTML (cleaned):
{body_html}

Return a JSON array of works performed at this concert. Each entry should be a
single string in the format "Composer: Work Title (opus/catalog number)" when
available, e.g.:
- "Johann Sebastian Bach: Weihnachts-Oratorium BWV 248"
- "Ludwig van Beethoven: Symphonie Nr. 9 d-moll op. 125"
- "Gustav Mahler: Symphonie Nr. 2 c-moll \\"Auferstehung\\""

Skip filler like "Pause" or "Einlass". If the page lists no works (e.g. an
opera evening that just states the opera title), return [] — the title is
already known.

Respond with ONLY the JSON array. No prose, no markdown fences.
"""
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        program = json.loads(text)
        if isinstance(program, list):
            return [str(p).strip() for p in program if p]
    except Exception as exc:
        print(f"  [claude] error for {event.get('id')}: {type(exc).__name__}: {str(exc)[:80]}")
    return []


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


def enrich_with_detail_programs(events: list[dict]) -> None:
    """Populate event['program'] by, in order:
       1. Reusing data from the previous gewandhaus_events.json (cache).
       2. Asking Claude (Haiku) to extract the program from the detail page,
          when ANTHROPIC_API_KEY is set.
       3. Falling back to static CSS-selector parsing.
    """
    cache = _load_program_cache()
    if cache:
        print(f"\nProgram cache: {len(cache)} previously-extracted entries")

    have_claude = _get_claude_client() is not None
    print(f"Claude API: {'enabled (Haiku)' if have_claude else 'disabled (no key)'}")

    cache_hits = 0
    for ev in events:
        if not ev.get("program") and ev.get("id") in cache:
            ev["program"] = cache[ev["id"]]
            cache_hits += 1
    if cache_hits:
        print(f"Reused {cache_hits} programs from previous run")

    todo = [e for e in events if not e.get("program") and e.get("url")]
    if not todo:
        print("\nAll events already have a program — nothing to fetch.")
        return

    print(f"\nFetching {len(todo)} detail pages for program extraction...")
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
        claude_attempted = False
        if have_claude:
            claude_attempted = True
            try:
                program = extract_program_with_claude(r.text, event)
            except Exception as exc:
                print(f"  [claude {eid}] EXCEPTION {type(exc).__name__}: {str(exc)[:120]}")
                claude_errors += 1
            if verbose:
                preview = program[0][:70] if program else "(empty)"
                print(f"  [claude {eid}] returned {len(program)} works: {preview}")
            if not program:
                claude_empty += 1

        if not program:
            soup = BeautifulSoup(r.text, "lxml")
            program = _extract_program_nodes(soup) or _extract_program_after_heading(soup)
            if program:
                enriched_static += 1
        elif claude_attempted:
            enriched_claude += 1

        if program:
            event["program"] = program
        elif not _DEBUG_DUMPED:
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
                  f"fetch_err:{fetch_errors} non200:{fetch_non_200} claude_err:{claude_errors} claude_empty:{claude_empty}")
        time.sleep(0.3)

    print(
        f"\nDetail pass summary:\n"
        f"  programs found: {enriched_claude} via Claude + {enriched_static} via selectors\n"
        f"  fetch failures: {fetch_errors} exceptions, {fetch_non_200} non-200 responses\n"
        f"  claude calls:   {claude_errors} errors, {claude_empty} returned empty list\n"
        f"  total processed: {len(todo)}"
    )


def parse_teasers(html: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    events = []
    for teaser in soup.find_all(class_="event-teaser"):
        event_id = parse_event_id(teaser)
        if not event_id:
            continue

        date_el = teaser.find("time", {"datetime": True})
        date = date_el["datetime"] if date_el else ""

        title, artists = parse_title_artists(teaser)
        series = parse_series(teaser)

        events.append({
            "id": event_id,
            "date": date,
            "time": parse_time(teaser),
            "title": series or title,
            "location": parse_location(teaser),
            "program": parse_program(teaser),
            "artists": artists,
            "url": parse_detail_url(teaser),
            "venue": "Gewandhaus Leipzig",
            "category": category.strip("/"),
            "source_url": BASE + category,
        })
    return events


def scrape_all(use_playwright: bool = True, max_clicks: int = 10) -> list[dict]:
    """Scrape all category pages using a single shared Playwright browser
    but a fresh browser context per category.

    Using browser.new_context() (rather than just browser.new_page()) means
    each category gets its own memory space. Closing the context after each
    category frees all JS heap, cached resources, and DOM trees — preventing
    the memory pressure that caused evaluate() to block on later categories
    (the root cause of the /tacheles/ hang).

    Playwright's sync_api uses greenlets bound to the calling thread; threads
    or multiprocessing cannot be mixed in without breaking it, so the loop
    stays single-threaded."""
    seen_ids: set[str] = set()
    all_events: list[dict] = []
    now = datetime.utcnow().isoformat() + "Z"

    playwright_ctx = None
    browser = None
    if use_playwright:
        try:
            from playwright.sync_api import sync_playwright
            playwright_ctx = sync_playwright().start()
            browser = playwright_ctx.chromium.launch(headless=True)
            print("Playwright browser started.")
        except ImportError:
            print("Playwright unavailable, using static fetch.")
            playwright_ctx = None
        except Exception as exc:
            print(f"Playwright launch failed: {exc} — using static fetch.")
            playwright_ctx = None

    try:
        for cat in CATEGORIES:
            url = BASE + cat
            print(f"  {cat:<26}", end="", flush=True)
            clicks = 0
            html = ""
            try:
                if browser is not None:
                    # Fresh context per category — closed after use so its
                    # JS heap and cached resources are freed immediately.
                    ctx = browser.new_context()
                    try:
                        page = ctx.new_page()
                        page.set_default_timeout(45000)
                        html, clicks = fetch_with_playwright_session(
                            page, url, max_clicks=max_clicks
                        )
                    except Exception as exc:
                        print(f"\n  Playwright error on {cat}: {str(exc)[:80]} — static fallback")
                        try:
                            html = fetch(url)
                        except Exception as fexc:
                            print(f"    static fetch also failed: {fexc}")
                            html = ""
                        clicks = 0
                    finally:
                        try:
                            ctx.close()  # frees all memory for this category
                        except Exception:
                            pass
                else:
                    html = fetch(url)

                events = parse_teasers(html, cat)
                new = 0
                for e in events:
                    if e["id"] not in seen_ids:
                        seen_ids.add(e["id"])
                        e["scraped_at"] = now
                        all_events.append(e)
                        new += 1
                tag = f"+{clicks} clicks " if clicks else ""
                print(f"  {tag}{len(events)} events ({new} new)")
            except Exception as exc:
                print(f"  ERROR for {cat}: {exc}")
            time.sleep(0.3)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_ctx is not None:
            try:
                playwright_ctx.stop()
            except Exception:
                pass

    return all_events


def main():
    parser = argparse.ArgumentParser(description="Gewandhaus Leipzig scraper")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Skip Playwright rendering; use plain requests")
    parser.add_argument("--no-detail", action="store_true",
                        help="Skip detail-page program extraction")
    parser.add_argument("--clicks", type=int, default=10,
                        help="Max 'Weitere laden' clicks per category (default 10)")
    args = parser.parse_args()

    print(f"Gewandhaus scraper — {len(CATEGORIES)} categories")
    print(f"  Playwright: {'off' if args.no_playwright else 'on'}, "
          f"max clicks: {args.clicks}, "
          f"detail pages: {'off' if args.no_detail else 'on'}")
    if not args.no_detail:
        _diagnose_claude_env()
    print("=" * 50)

    events = scrape_all(use_playwright=not args.no_playwright,
                       max_clicks=args.clicks)
    events.sort(key=lambda e: e.get("date") or "9999-99-99")
    print(f"\nTotal unique events: {len(events)}")

    if not args.no_detail:
        enrich_with_detail_programs(events)

    out = Path(__file__).parent.parent / "gewandhaus_events.json"
    payload = {
        "total_events": len(events),
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "events": events,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Saved → {out}")


if __name__ == "__main__":
    main()
