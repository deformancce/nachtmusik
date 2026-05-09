"""
Universal venue scraper using Playwright + the Anthropic API.

Strategy:
  1. Render the venue's calendar page with Playwright. This handles JS-driven
     lazy loading, including clicking "Weitere Veranstaltungen laden" / "Load
     more" buttons up to N times.
  2. Strip noise (scripts, styles, comments) and send the body HTML to Claude.
     Claude returns a list of events as JSON.
  3. For each event with a detail URL, fetch the detail page (plain requests)
     and ask Claude to extract the full program (composer + work + opus).

Requires:
  pip3 install anthropic playwright
  playwright install chromium
  export ANTHROPIC_API_KEY="sk-ant-..."

Examples:
  python3 scrape/smart_venue_scraper.py --list
  python3 scrape/smart_venue_scraper.py --venue "Gewandhaus"
  python3 scrape/smart_venue_scraper.py --venue "Gewandhaus" --clicks 10
  python3 scrape/smart_venue_scraper.py --venue "Gewandhaus" --no-program
  python3 scrape/smart_venue_scraper.py --url https://example.com/x --name "Test"
  python3 scrape/smart_venue_scraper.py --all --detail-limit 5
"""
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
import argparse
import json
import os
import sys
import time

import requests
from bs4 import BeautifulSoup, Comment

try:
    import anthropic
except ImportError:
    print("Install anthropic: pip3 install anthropic")
    sys.exit(1)

# Allow running this file as a script (python3 scrape/smart_venue_scraper.py)
sys.path.insert(0, str(Path(__file__).parent.parent))
from venues_germany import VENUES_GERMANY  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; op.us/0.1)"
}
MODEL = "claude-sonnet-4-6"
MAX_LISTING_HTML_CHARS = 150_000   # listing pages can be large after lazy-load
MAX_DETAIL_HTML_CHARS = 30_000     # detail pages are usually small
LOAD_MORE_KEYWORDS = [
    "Weitere Veranstaltungen",
    "Weitere laden",
    "Mehr Veranstaltungen",
    "Mehr laden",
    "Mehr anzeigen",
    "Load more",
    "Show more",
    "More events",
]


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def _strip_noise(html: str) -> str:
    """Remove scripts/styles/comments and return the cleaned body HTML."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    body = soup.find("body")
    return str(body) if body else str(soup)


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8 bytes that were decoded as Windows-1252.

    e.g. "GroÃŸer" -> "Großer", "PÃ¤rt" -> "Pärt", "â€”" -> "—".
    Safe no-op for already-clean strings (round-trip fails -> original kept).
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        return text.encode("windows-1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _fix_event(event: Dict) -> Dict:
    """Apply mojibake repair to all string fields in an event dict."""
    for key, value in list(event.items()):
        if isinstance(value, str):
            event[key] = _fix_mojibake(value)
        elif isinstance(value, list):
            event[key] = [
                _fix_mojibake(v) if isinstance(v, str) else v for v in value
            ]
    return event


def fetch_static(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    # Force UTF-8 unless the server explicitly says otherwise. requests falls
    # back to ISO-8859-1 when there's no charset header, which mangles German
    # umlauts and em dashes on most modern sites.
    if not response.encoding or response.encoding.lower() in ("iso-8859-1", "latin-1"):
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def fetch_rendered(url: str, max_clicks: int = 5) -> str:
    """Render with Playwright and click 'load more' buttons up to max_clicks times."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip3 install playwright && playwright install chromium")
        raise

    print(f"  Rendering with Playwright (max {max_clicks} load-more clicks)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(60000)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        clicks = 0
        for _ in range(max_clicks):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            clicked_text = page.evaluate(
                """
                (keywords) => {
                    const elements = document.querySelectorAll('a, button');
                    for (const el of elements) {
                        const text = (el.textContent || '').trim();
                        if (!text) continue;
                        const matches = keywords.some(kw => text.includes(kw));
                        if (matches && el.offsetParent !== null) {
                            el.click();
                            return text;
                        }
                    }
                    return null;
                }
                """,
                LOAD_MORE_KEYWORDS,
            )
            if not clicked_text:
                break
            clicks += 1
            print(f"    Clicked: {clicked_text[:50]}")
            time.sleep(3)

        if clicks:
            print(f"  Loaded {clicks} extra batch{'es' if clicks != 1 else ''}.")
        html = page.content()
        browser.close()
    return html


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

def _client() -> "anthropic.Anthropic":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Export it before running this scraper."
        )
    return anthropic.Anthropic(api_key=api_key)


def _parse_json_response(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json"):]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return json.loads(text)


def extract_events_with_claude(
    client: "anthropic.Anthropic",
    html: str,
    venue_name: str,
    url: str,
) -> List[Dict]:
    body_html = _strip_noise(html)[:MAX_LISTING_HTML_CHARS]
    prompt = f"""You are extracting structured concert event data from a classical music venue website.

Venue: {venue_name}
URL: {url}

HTML (cleaned, scripts/styles removed):
{body_html}

For EVERY concert/opera/recital event you can find, return one JSON object with:
- date: "YYYY-MM-DD" if possible, else best available text
- time: "HH:MM" if available, else ""
- title: concert title (e.g. "Grosses Concert", "Tristan und Isolde", "Liederabend")
- location: hall / room name (else "")
- program: array of works/composers as listed on the page (e.g. ["Brahms", "Mahler"]
  or ["Bach: Weihnachts-Oratorium BWV 248"]). [] if not stated on the listing.
- artists: array of orchestras / soloists / conductors. [] if none.
- url: detail page URL — ABSOLUTE if possible, else "" .

Output ONLY a JSON array. No prose, no markdown fences. If you find no events, return [].
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text
    try:
        events = _parse_json_response(text)
    except json.JSONDecodeError as exc:
        print(f"  JSON parse error on listing: {exc}")
        return []

    if not isinstance(events, list):
        return []

    base = url
    now = datetime.utcnow().isoformat() + "Z"
    cleaned: List[Dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event = _fix_event(event)
        event["venue"] = venue_name
        event["source_url"] = url
        event["scraped_at"] = now
        # Make detail URLs absolute when relative
        if event.get("url"):
            event["url"] = urljoin(base, event["url"])
        cleaned.append(event)
    return cleaned


def extract_program_with_claude(
    client: "anthropic.Anthropic",
    html: str,
    event: Dict,
) -> List[str]:
    body_html = _strip_noise(html)[:MAX_DETAIL_HTML_CHARS]
    prompt = f"""Extract the concert program from this detail page.

Concert title: {event.get('title', '')}
Date: {event.get('date', '')}

HTML (cleaned):
{body_html}

Return a JSON array of works performed. Each entry should be a string in the
format "Composer: Work Title (opus/catalog number)" when available, e.g.:
- "Johann Sebastian Bach: Weihnachts-Oratorium BWV 248"
- "Ludwig van Beethoven: Symphonie Nr. 9 d-moll op. 125"
- "Gustav Mahler: Symphonie Nr. 2 'Auferstehung'"

Output ONLY a JSON array. If no program is listed, return [].
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text
    try:
        program = _parse_json_response(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(program, list):
        return []
    return [_fix_mojibake(str(p)) for p in program if p]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape_venue(
    client: "anthropic.Anthropic",
    venue: Dict,
    *,
    use_browser: bool = True,
    max_clicks: int = 5,
    fetch_program: bool = True,
    detail_limit: int = 20,
) -> List[Dict]:
    name = venue["name"]
    url = venue["url"]
    print(f"\n{name}")
    print(f"  {url}")

    try:
        if use_browser:
            html = fetch_rendered(url, max_clicks=max_clicks)
        else:
            html = fetch_static(url)
        print(f"  Loaded {len(html)} chars")
    except Exception as exc:
        print(f"  Fetch failed: {str(exc)[:120]}")
        return []

    try:
        events = extract_events_with_claude(client, html, name, url)
        print(f"  Found {len(events)} events")
    except Exception as exc:
        print(f"  Listing extraction failed: {str(exc)[:120]}")
        return []

    if not fetch_program or not events:
        return events

    detail_targets = [e for e in events if e.get("url")][:detail_limit]
    if not detail_targets:
        print("  No detail URLs to follow.")
        return events

    print(f"  Fetching {len(detail_targets)} detail page(s) for program info...")
    for i, event in enumerate(detail_targets, 1):
        try:
            detail_html = fetch_static(event["url"])
            program = extract_program_with_claude(client, detail_html, event)
            if program:
                event["program_detailed"] = program
                print(f"    {i}/{len(detail_targets)} {event.get('title', '')[:40]}: {len(program)} works")
            else:
                print(f"    {i}/{len(detail_targets)} {event.get('title', '')[:40]}: no program found")
            # Be polite
            time.sleep(0.5)
        except Exception as exc:
            print(f"    {i}/{len(detail_targets)} detail fetch failed: {str(exc)[:80]}")

    return events


def scrape_venues(
    venues: List[Dict],
    **kwargs,
) -> Dict:
    client = _client()
    print("=" * 70)
    print(f"Smart venue scraper — {len(venues)} venue(s)")
    print(f"Model: {MODEL}")
    print("=" * 70)

    all_events: List[Dict] = []
    successful = 0
    failed = 0
    for venue in venues:
        events = scrape_venue(client, venue, **kwargs)
        if events:
            all_events.extend(events)
            successful += 1
        else:
            failed += 1

    print("\n" + "=" * 70)
    print(f"Successful: {successful}/{len(venues)}")
    print(f"Failed:     {failed}")
    print(f"Events:     {len(all_events)}")

    return {
        "total_events": len(all_events),
        "successful_venues": successful,
        "failed_venues": failed,
        "model": MODEL,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "events": all_events,
    }


def save_results(result: Dict, filename: str) -> Path:
    output_path = Path(__file__).parent.parent / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _find_venues_by_name(needle: str) -> List[Dict]:
    needle = needle.lower()
    return [v for v in VENUES_GERMANY if needle in v["name"].lower()]


def _print_venues() -> None:
    print(f"{len(VENUES_GERMANY)} configured venues:\n")
    by_tier: Dict = {}
    for v in VENUES_GERMANY:
        by_tier.setdefault(str(v["tier"]), []).append(v)
    for tier in sorted(by_tier.keys()):
        print(f"  Tier {tier}:")
        for v in by_tier[tier]:
            print(f"    - {v['name']:<45} {v['url']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart venue scraper (Playwright + Claude API)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Scrape all configured venues")
    group.add_argument("--venue", help="Scrape one venue by substring of its name")
    group.add_argument("--url", help="Scrape an arbitrary URL (use with --name)")
    group.add_argument("--list", action="store_true", help="List configured venues and exit")

    parser.add_argument("--name", default="Custom venue", help="Venue label when using --url")
    parser.add_argument("--clicks", type=int, default=5,
                        help="Max 'load more' clicks during page render (default: 5)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip Playwright; use plain requests (for static sites)")
    parser.add_argument("--no-program", action="store_true",
                        help="Skip detail-page program extraction (cheaper, faster)")
    parser.add_argument("--detail-limit", type=int, default=20,
                        help="Max detail pages to fetch per venue (default: 20)")
    parser.add_argument("--output", default=None,
                        help="Output JSON filename (default depends on mode)")
    args = parser.parse_args()

    if args.list:
        _print_venues()
        return

    if args.url:
        target_venues = [{"name": args.name, "city": "", "url": args.url, "tier": "custom"}]
        default_output = "single_venue_events.json"
    elif args.venue:
        target_venues = _find_venues_by_name(args.venue)
        if not target_venues:
            print(f"No venue matches '{args.venue}'. Try --list to see all venues.")
            sys.exit(1)
        if len(target_venues) > 1:
            print(f"Matched {len(target_venues)} venues:")
            for v in target_venues:
                print(f"  - {v['name']}")
            print("\nNarrow your --venue argument to pick exactly one.")
            sys.exit(1)
        default_output = "single_venue_events.json"
    elif args.all:
        target_venues = VENUES_GERMANY
        default_output = "all_venues_events.json"
    else:
        target_venues = VENUES_GERMANY[:1]
        default_output = "single_venue_events.json"
        print("(No mode given — defaulting to a single venue smoke test. Use --help for options.)\n")

    result = scrape_venues(
        target_venues,
        use_browser=not args.no_browser,
        max_clicks=args.clicks,
        fetch_program=not args.no_program,
        detail_limit=args.detail_limit,
    )
    save_results(result, args.output or default_output)


if __name__ == "__main__":
    main()
