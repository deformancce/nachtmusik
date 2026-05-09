"""
Universal venue scraper using the Anthropic API.

For each venue in venues_germany.VENUES_GERMANY:
  1. Fetch the calendar HTML page
  2. Send the body HTML to Claude with a prompt asking for events
  3. Parse the JSON response, normalize, and aggregate

Requires:
  export ANTHROPIC_API_KEY="sk-ant-..."
"""
from typing import List, Dict
from datetime import datetime
from pathlib import Path
import json
import os
import sys

import requests
from bs4 import BeautifulSoup

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
MODEL = "claude-sonnet-4-5"
MAX_HTML_CHARS = 50000


def _client() -> "anthropic.Anthropic":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Export it before running this scraper."
        )
    return anthropic.Anthropic(api_key=api_key)


def fetch_page_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def _trim_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body")
    text = str(body) if body else html
    return text[:MAX_HTML_CHARS]


def extract_events_with_claude(
    client: "anthropic.Anthropic",
    html: str,
    venue_name: str,
    url: str,
) -> List[Dict]:
    body_html = _trim_html(html)
    prompt = f"""You are an expert at extracting structured concert event data from
HTML pages of European classical music venues.

Venue: {venue_name}
URL: {url}

HTML:
{body_html}

For every concert/opera event you find, return an object with these fields:
- date: YYYY-MM-DD if possible
- time: HH:MM if available, else ""
- title: concert title
- location: hall / room name (else "")
- program: array of works performed (composer + work), [] if not stated
- artists: array of orchestras / soloists / conductors, [] if not stated
- url: detail page URL (absolute), else ""

Output ONLY a JSON array. No prose, no markdown fences. If no events are found,
return [].
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    # Strip markdown fences defensively
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json"):]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        events = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  JSON parse error: {exc}")
        return []

    if not isinstance(events, list):
        return []

    for event in events:
        event["venue"] = venue_name
        event["source_url"] = url
        event["scraped_at"] = datetime.utcnow().isoformat() + "Z"
    return events


def scrape_venue(client: "anthropic.Anthropic", venue: Dict) -> List[Dict]:
    name = venue["name"]
    url = venue["url"]
    print(f"\n{name}")
    print(f"  {url}")
    try:
        html = fetch_page_html(url)
        print(f"  Loaded {len(html)} chars")
    except Exception as exc:
        print(f"  Fetch failed: {str(exc)[:80]}")
        return []
    try:
        events = extract_events_with_claude(client, html, name, url)
        print(f"  Found {len(events)} events")
        return events
    except Exception as exc:
        print(f"  Extraction failed: {str(exc)[:80]}")
        return []


def scrape_all_venues(venues: List[Dict] = None) -> Dict:
    venues = venues or VENUES_GERMANY
    client = _client()

    print("=" * 70)
    print(f"Smart venue scraper — {len(venues)} venues")
    print("=" * 70)

    all_events: List[Dict] = []
    successful = 0
    failed = 0
    for venue in venues:
        events = scrape_venue(client, venue)
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
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "events": all_events,
    }


def save_results(result: Dict, filename: str = "all_venues_events.json") -> Path:
    output_path = Path(__file__).parent.parent / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to: {output_path}")
    return output_path


if __name__ == "__main__":
    result = scrape_all_venues()
    save_results(result)
