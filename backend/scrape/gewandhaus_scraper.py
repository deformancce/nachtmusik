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


def fetch_with_playwright(url: str, max_clicks: int = 10) -> tuple[str, int]:
    """Render the URL with a headless browser, click 'Weitere laden' up to
    max_clicks times, and return the final HTML + the number of successful
    clicks. Raises ImportError if Playwright isn't installed."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(60000)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            clicks = 0
            consecutive_no_button = 0
            for _ in range(max_clicks):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.6)
                clicked = page.evaluate(
                    """
                    (keywords) => {
                        const elements = document.querySelectorAll('a, button');
                        for (const el of elements) {
                            const text = (el.textContent || '').trim();
                            if (!text) continue;
                            if (keywords.some(kw => text.includes(kw)) && el.offsetParent !== null) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                    """,
                    list(LOAD_MORE_KEYWORDS),
                )
                if clicked:
                    clicks += 1
                    consecutive_no_button = 0
                    time.sleep(2.2)
                else:
                    consecutive_no_button += 1
                    if consecutive_no_button >= 2:
                        break
                    time.sleep(0.8)

            html = page.content()
        finally:
            browser.close()
    return html, clicks


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


def fetch_program_from_detail(url: str) -> list[str]:
    """Fetch a single event's detail page and extract its program (works performed)."""
    if not url:
        return []
    try:
        html = fetch(url)
    except Exception:
        return []
    soup = BeautifulSoup(html, "lxml")
    program = _extract_program_nodes(soup) or _extract_program_after_heading(soup)
    return program


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
    """Scrape all category pages.

    use_playwright=True clicks 'Weitere Veranstaltungen laden' up to max_clicks
    times per category to expose lazy-loaded events. On ImportError it falls
    back to a single static fetch (which only sees the initial events)."""
    seen_ids: set[str] = set()
    all_events: list[dict] = []
    now = datetime.utcnow().isoformat() + "Z"
    playwright_ok = use_playwright

    for cat in CATEGORIES:
        url = BASE + cat
        print(f"  {cat:<26}", end="", flush=True)
        try:
            if playwright_ok:
                try:
                    html, clicks = fetch_with_playwright(url, max_clicks=max_clicks)
                except ImportError:
                    print(" (Playwright unavailable, falling back to static fetch)")
                    playwright_ok = False
                    html = fetch(url)
                    clicks = 0
                except Exception as exc:
                    print(f"  Playwright error: {str(exc)[:60]} — static fallback")
                    html = fetch(url)
                    clicks = 0
            else:
                html = fetch(url)
                clicks = 0

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
            print(f"  ERROR: {exc}")
        time.sleep(0.4)

    return all_events


def enrich_with_detail_programs(events: list[dict]) -> None:
    """For every event without a listing-derived program, fetch its detail page."""
    todo = [e for e in events if not e.get("program") and e.get("url")]
    if not todo:
        return
    print(f"\nFetching detail pages for {len(todo)} events (program extraction)...")
    enriched = 0
    for i, event in enumerate(todo, 1):
        program = fetch_program_from_detail(event["url"])
        if program:
            event["program"] = program
            enriched += 1
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} processed, {enriched} with program so far")
        time.sleep(0.3)
    print(f"Detail pass complete: {enriched}/{len(todo)} events got a program")


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
