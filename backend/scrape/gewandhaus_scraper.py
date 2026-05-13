"""
Gewandhaus Leipzig scraper — API-key-free, Playwright-free.

Fetches all concert category pages from gewandhausorchester.de,
parses the hidden-but-present event teasers, deduplicates by event ID,
and writes backend/gewandhaus_events.json.

Usage:
  python3 scrape/gewandhaus_scraper.py
"""
from datetime import datetime
from pathlib import Path
import json
import time
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE = "https://www.gewandhausorchester.de"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; op.us/0.1)"}

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


def parse_time(teaser) -> str:
    el = teaser.find(class_=lambda c: c and "event-teaser__time" in " ".join(c if isinstance(c, list) else [c]))
    if not el:
        return ""
    raw = el.get_text(strip=True)  # e.g. "19.30 Uhr"
    return raw.replace(" Uhr", "").replace(".", ":")  # → "19:30"


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
    # Siblings of h2 that are <p> tags contain soloists
    artists = [title]
    for sib in h2.find_next_siblings("p"):
        # Stop at program-related blocks
        parent_classes = " ".join(sib.parent.get("class", []))
        if "details-level-1" in parent_classes or "details-hide" in parent_classes:
            break
        txt = sib.get_text(" ", strip=True)
        if txt and "Werke von" not in txt:
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


def scrape_all() -> list[dict]:
    seen_ids: set[str] = set()
    all_events: list[dict] = []
    now = datetime.utcnow().isoformat() + "Z"

    for cat in CATEGORIES:
        url = BASE + cat
        print(f"  {cat}", end="", flush=True)
        try:
            html = fetch(url)
            events = parse_teasers(html, cat)
            new = 0
            for e in events:
                if e["id"] not in seen_ids:
                    seen_ids.add(e["id"])
                    e["scraped_at"] = now
                    all_events.append(e)
                    new += 1
            print(f"  {len(events)} events ({new} new)")
        except Exception as exc:
            print(f"  ERROR: {exc}")
        time.sleep(0.4)

    return all_events


def main():
    print(f"Gewandhaus scraper — {len(CATEGORIES)} categories")
    print("=" * 50)
    events = scrape_all()
    events.sort(key=lambda e: e.get("date") or "9999-99-99")
    print(f"\nTotal unique events: {len(events)}")

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
