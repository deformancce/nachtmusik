"""
Firecrawl-based scraper — smoke test for Tier-1 venues.

Calls Firecrawl's /extract endpoint with a Pydantic event schema for each
Tier-1 venue listing and writes the structured result to
backend/firecrawl_<slug>_events.json (separate filenames from the existing
custom scraper output so both can coexist for comparison).

Free-tier plan: each venue = 1 extract call, so 13 venues = 13 credits.

Usage:
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper --only konzerthaus_berlin
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper --max-events 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE.parent))
from backend.venues_germany import get_venues_by_tier


class Event(BaseModel):
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    time: Optional[str] = Field(None, description="HH:MM in 24-hour format")
    title: str = Field(..., description="Concert title (not the ticket button)")
    venue_hall: Optional[str] = Field(None, description="Hall name within the venue, e.g. 'Großer Saal'")
    program: list[str] = Field(
        default_factory=list,
        description="Works as 'Composer Lastname: Full Work Title with opus/catalog'",
    )
    performers: list[str] = Field(
        default_factory=list,
        description="Soloists and ensemble names. Do NOT include the conductor here.",
    )
    conductor: Optional[str] = None
    price: Optional[str] = Field(
        None,
        description="Ticket price as shown on the page, e.g. 'ab €25', '€25–€85', '€15 / erm. €8'",
    )
    detail_url: Optional[str] = Field(None, description="Absolute URL of the event detail page")


class EventList(BaseModel):
    total_events_visible: Optional[int] = Field(
        None,
        description="Total number of upcoming concerts visible on this listing page, including any beyond the 5 returned in 'events'.",
    )
    events: list[Event]


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.translate(_UMLAUTS).lower()).strip("_")


def _build_prompt(venue: dict, max_events: int) -> str:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return (
        f"This is the concert listing page of {venue['name']} in {venue['city']}, Germany.\n"
        f"Today's date is {today}. Only consider events on or after this date — IGNORE any "
        "past/archived events. If a date field is missing or older than today, skip that entry.\n\n"
        "TASK in two steps:\n"
        f"1. Count ALL upcoming concerts visible on this page (could be 10, 30, 100+). "
        "Put the total into 'total_events_visible'.\n"
        f"2. From those, return the next {max_events} concerts (chronologically nearest from today) "
        "with COMPLETE data in the 'events' array.\n\n"
        "For each of those events, populate ALL these fields if the page shows them:\n"
        "  - date (YYYY-MM-DD), time (HH:MM 24h)\n"
        "  - title (the concert/programme name — NOT a ticket button or generic label like 'Konzert')\n"
        "  - venue_hall (e.g. 'Großer Saal', 'Mozart-Saal', 'Isarphilharmonie')\n"
        "  - program: list of works as 'Composer Lastname: Full Work Title with opus' "
        "(e.g. 'Brahms: Symphonie Nr. 1 c-Moll op. 68')\n"
        "  - performers: soloists + orchestra/ensemble names. Do NOT put the conductor here.\n"
        "  - conductor: name only, no role suffix\n"
        "  - price: ticket price exactly as shown ('ab €25', '€25–€85', '€15 / erm. €8'). "
        "If only a range is on the listing, use that.\n"
        "  - detail_url: absolute URL of the event's own detail page on this venue's site\n\n"
        "STRICT RULES:\n"
        f"- Only include events AT {venue['name']} in {venue['city']}. Skip guest tours of the resident "
        "orchestra to other cities, and skip cross-promotion of other venues.\n"
        "- NEVER invent program or performer entries. If the listing page does not show them, "
        "leave those arrays EMPTY. Do not guess based on the title alone — a title like "
        "'Die Prinzen Symphonisch' is a pop crossover, not a Mahler symphony.\n"
        "- Skip non-music entries: theater, dance, lectures, masterclasses, family workshops, "
        "navigation links, season-pass entries.\n"
        "- Skip events that are clearly tickets-only listings without a real concert title."
    )


def _scrape_one(app, venue: dict, max_events: int) -> dict:
    slug = _slug(venue["name"])
    url = venue["url"]
    print(f"  Firecrawl scraping {venue['name']} ({url}) ...", flush=True)

    prompt = _build_prompt(venue, max_events)
    schema = EventList.model_json_schema()

    payload = {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "engine": "firecrawl",
    }

    # Firecrawl Python SDK v2 renamed scrape_url → scrape and reshaped its
    # extract() signature. Try the modern patterns first; fall back to legacy.
    extracted: dict = {}
    last_err: Exception | None = None
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    attempts = (
        # v2 SDK: scrape() with formats=[{type: 'json', ...}]
        ("scrape v2 formats-list", lambda: app.scrape(
            url, formats=[json_fmt])),
        ("scrape v2 formats-list+markdown", lambda: app.scrape(
            url, formats=["markdown", json_fmt])),
        # v2 SDK: scrape() with formats=["json"] and json_options
        ("scrape v2 json_options", lambda: app.scrape(
            url, formats=["json"], json_options={"schema": schema, "prompt": prompt})),
        # v2 SDK: extract() with kwargs only
        ("extract v2 kwargs", lambda: app.extract(
            urls=[url], schema=schema, prompt=prompt)),
        ("extract v2 positional+kwargs", lambda: app.extract(
            [url], schema=schema, prompt=prompt)),
        # v1 legacy
        ("scrape_url legacy", lambda: app.scrape_url(
            url, formats=["extract"],
            extract={"schema": schema, "prompt": prompt})),
    )

    for label, fn in attempts:
        try:
            result = fn()
        except TypeError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            print(f"    [warn] {label} raised: {e}")
            continue

        # Normalise response shape
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if isinstance(result, dict):
            extracted = (
                result.get("json")
                or result.get("extract")
                or (result.get("data") or {}).get("json")
                or (result.get("data") or {}).get("extract")
                or result.get("data")
                or {}
            )
        if extracted:
            payload["_strategy"] = label
            break

    events_raw = extracted.get("events") if isinstance(extracted, dict) else None
    total_visible = extracted.get("total_events_visible") if isinstance(extracted, dict) else None
    if not events_raw:
        payload["error"] = f"no events extracted (last error: {last_err})" if last_err else "no events extracted"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_visible"] = total_visible
        return payload

    events: list[dict] = []
    for e in events_raw[:max_events]:
        if not isinstance(e, dict):
            continue
        events.append({
            "date": e.get("date"),
            "time": e.get("time"),
            "title": (e.get("title") or "").strip(),
            "venue_hall": e.get("venue_hall"),
            "program": e.get("program") or [],
            "performers": e.get("performers") or [],
            "conductor": e.get("conductor"),
            "price": e.get("price"),
            "detail_url": e.get("detail_url"),
            "venue": venue["name"],
            "city": venue["city"],
        })

    payload["events"] = events
    payload["total_events"] = len(events)
    payload["total_events_visible"] = total_visible
    return payload


def main(slugs_filter: list[str] | None, max_events: int) -> None:
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print("ERROR: FIRECRAWL_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)

    try:
        import firecrawl as _fc_module
    except ImportError:
        print("ERROR: firecrawl-py not installed. Run: pip install firecrawl-py",
              file=sys.stderr)
        sys.exit(1)

    # Prefer the v2 class name, fall back to the legacy alias.
    ClientCls = getattr(_fc_module, "Firecrawl", None) or getattr(_fc_module, "FirecrawlApp", None)
    if ClientCls is None:
        print("ERROR: neither Firecrawl nor FirecrawlApp found in firecrawl package",
              file=sys.stderr)
        sys.exit(1)

    app = ClientCls(api_key=api_key)
    print(f"firecrawl-py version: {getattr(_fc_module, '__version__', 'unknown')}")
    print(f"Client class: {ClientCls.__name__}")
    methods = sorted(m for m in dir(app) if not m.startswith("_") and callable(getattr(app, m, None)))
    print(f"Client methods: {methods}\n")
    venues = get_venues_by_tier(1)
    if slugs_filter:
        venues = [v for v in venues if _slug(v["name"]) in slugs_filter]

    if not venues:
        print("No venues matched the filter.", file=sys.stderr)
        sys.exit(1)

    print(f"Firecrawl scraping {len(venues)} Tier-1 venue(s), max {max_events} events each.")
    n_ok = 0
    n_fail = 0
    for venue in venues:
        result = _scrape_one(app, venue, max_events)
        out_path = BASE / f"firecrawl_{result['slug']}_events.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("error"):
            n_fail += 1
            print(f"    FAIL: {result['error']}")
        else:
            n_ok += 1
            tv = result.get("total_events_visible")
            tv_str = f" (of {tv} visible)" if tv else ""
            print(f"    OK: {result['total_events']} events{tv_str} -> {out_path.name}")

    print(f"\nDone. ok={n_ok}  fail={n_fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="SLUG",
                        help="Run only on these venue slugs")
    parser.add_argument("--max-events", type=int, default=5,
                        help="Cap events per venue (default 5)")
    args = parser.parse_args()
    main(args.only, args.max_events)
