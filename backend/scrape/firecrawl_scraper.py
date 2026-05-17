"""
Firecrawl-based scraper for Tier-1 concert venues.

Two-phase approach per venue:
  Phase 1 — Discovery: map() the site to find ALL event URLs (1 credit).
             Also scrape the listing page with scroll actions (1 credit) to
             get the next N events in date order.
  Phase 2 — Enrichment: scrape each event detail page for full data
             (program, performers, conductor, price). (N credits, default 5)

Total credits per venue: ~7 (1 map + 1 listing + 5 details)
Total for all 13 Tier-1 venues: ~91 credits/run

Usage:
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper --only konzerthaus_berlin
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper --max-events 10
    FIRECRAWL_API_KEY=fc-... python3 -m backend.scrape.firecrawl_scraper --skip-enrich  # listing only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

from pydantic import BaseModel, Field

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE.parent))
from backend.venues_germany import get_venues_by_tier
from backend.scrape.url_filters import filter_event_urls


# ── Pydantic schemas ──────────────────────────────────────────────────────────

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
        description="Total number of upcoming concerts visible on this listing page (including those beyond the N returned in 'events').",
    )
    events: list[Event]


class EventDetail(BaseModel):
    title: Optional[str] = Field(None, description="Concert title")
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    time: Optional[str] = Field(None, description="HH:MM in 24-hour format")
    venue_hall: Optional[str] = Field(None, description="Hall name, e.g. 'Großer Saal'")
    program: list[str] = Field(
        default_factory=list,
        description="Works as 'Composer Lastname: Full Work Title with opus/catalog'",
    )
    performers: list[str] = Field(
        default_factory=list,
        description="Soloists and ensemble names. Do NOT include the conductor here.",
    )
    conductor: Optional[str] = None
    price: Optional[str] = Field(None, description="Ticket price, e.g. 'ab €25'")
    duration_min: Optional[int] = Field(None, description="Total duration in minutes if shown")


# ── Helpers ───────────────────────────────────────────────────────────────────

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.translate(_UMLAUTS).lower()).strip("_")


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _normalise(result) -> dict:
    """Flatten a Firecrawl response into a plain dict."""
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        return {}
    return (
        result.get("json")
        or result.get("extract")
        or (result.get("data") or {}).get("json")
        or (result.get("data") or {}).get("extract")
        or result.get("data")
        or {}
    )


# ── Scroll actions (for infinite-scroll / lazy-load listing pages) ────────────

def _scroll_actions(n: int = 12, amount: int = 3000, wait_ms: int = 700) -> list[dict]:
    """Generate Firecrawl scroll+wait actions to trigger lazy loading."""
    actions = []
    for _ in range(n):
        actions.append({"type": "scroll", "direction": "down", "amount": amount})
        actions.append({"type": "wait", "milliseconds": wait_ms})
    return actions


# ── Phase 1a: Listing scrape with scroll (date-sorted top-N events) ───────────

def _build_listing_prompt(venue: dict, max_events: int) -> str:
    today = _today()
    return (
        f"This is the concert listing page of {venue['name']} in {venue['city']}, Germany.\n"
        f"Today's date is {today}. ONLY consider events strictly on or after {today} — "
        "skip any event with a date before today.\n\n"
        "TASK (two parts):\n"
        f"1. Count ALL upcoming concerts visible on this fully-scrolled page. "
        "Write the total into 'total_events_visible'.\n"
        f"2. Return the next {max_events} upcoming concerts (sorted by date, nearest first) "
        "in the 'events' array with COMPLETE data.\n\n"
        "For each event fill ALL visible fields:\n"
        "  date (YYYY-MM-DD), time (HH:MM 24h), title (concert name, NOT a ticket button),\n"
        "  venue_hall (e.g. 'Großer Saal'), program (list of 'Composer: Title op.X'),\n"
        "  performers (soloists + ensemble, NOT conductor), conductor, price (e.g. 'ab €25'),\n"
        "  detail_url (absolute URL of the event's own page on this venue's website).\n\n"
        "RULES:\n"
        f"- Only events physically AT {venue['name']} in {venue['city']}. "
        "Skip guest tours to other cities and cross-promotion of other venues.\n"
        "- NEVER invent program/performer data. If not shown on the listing, leave arrays EMPTY.\n"
        "  A title like 'Die Prinzen Symphonisch' is a pop show — do NOT invent a Mahler symphony.\n"
        "- Skip non-concert entries: theater, dance, lectures, navigation items, season-pass upsells.\n"
        "- detail_url must be an absolute URL (start with https://). If you cannot find one, use null."
    )


def _build_detail_prompt(venue: dict) -> str:
    today = _today()
    return (
        f"This is an event detail page for a concert at {venue['name']} in {venue['city']}, Germany.\n"
        f"Today is {today}.\n"
        "Extract ALL of the following from the page:\n"
        "  title, date (YYYY-MM-DD), time (HH:MM 24h), venue_hall (e.g. 'Großer Saal'),\n"
        "  program (list of 'Composer Lastname: Full Work Title with opus', e.g. "
        "'Brahms: Symphonie Nr. 1 c-Moll op. 68'),\n"
        "  performers (soloists + ensemble names — NOT the conductor),\n"
        "  conductor (name only), price (e.g. 'ab €25', '€15–€85'), duration_min.\n"
        "NEVER invent data. Only extract what is explicitly written on this page."
    )


_DE_HEADERS = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"}


def _scrape_listing(app, venue: dict, max_events: int) -> dict:
    """
    Scrape the listing page with scroll actions.
    Returns the raw Firecrawl extraction dict or {}.
    """
    prompt = _build_listing_prompt(venue, max_events)
    schema = EventList.model_json_schema()
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    actions = _scroll_actions()

    last_err: Exception | None = None

    # Try with scroll actions first, then without (some sites reject action requests)
    for with_actions in (True, False):
        extra: dict = {"headers": _DE_HEADERS}
        if with_actions:
            extra["actions"] = actions
        try:
            result = app.scrape(venue["url"], formats=[json_fmt], **extra)
            extracted = _normalise(result)
            if extracted.get("events"):
                return extracted
        except TypeError as e:
            # unexpected kwarg (e.g. SDK doesn't accept headers) — retry without it
            if "headers" in str(e):
                extra.pop("headers", None)
                try:
                    result = app.scrape(venue["url"], formats=[json_fmt], **extra)
                    extracted = _normalise(result)
                    if extracted.get("events"):
                        return extracted
                except Exception as e2:
                    last_err = e2
        except Exception as e:
            last_err = e
            print(f"    [warn] listing scrape ({'with' if with_actions else 'without'} scroll): {e}")

    return {}


# ── Phase 1b: Site-wide discovery via map() ───────────────────────────────────

def _discover_all_event_urls(app, venue: dict) -> tuple[list[str], list[str]]:
    """
    Use Firecrawl map() to discover event-detail URLs on the venue site.
    Returns (loose_urls, strict_urls). Non-fatal on failure.
    """
    slug = _slug(venue["name"])
    try:
        result = app.map(venue["url"])
        if hasattr(result, "links"):
            links = result.links or []
        elif isinstance(result, dict):
            links = result.get("links") or []
        else:
            links = []
        loose = filter_event_urls(links, venue["url"], strict=False)
        strict = filter_event_urls(links, venue["url"], venue_slug=slug, strict=True)
        if loose or strict:
            print(
                f"    [map] discovered URLs: loose={len(loose)} strict={len(strict)}",
                flush=True,
            )
        return loose, strict
    except Exception as e:
        print(f"    [map] failed (non-fatal): {e}", flush=True)
        return [], []


# ── Phase 2: Enrich event detail pages ───────────────────────────────────────

def _scrape_one_detail(app, detail_url: str, venue: dict) -> dict:
    """Scrape a single event detail page and return structured data."""
    prompt = _build_detail_prompt(venue)
    schema = EventDetail.model_json_schema()
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    try:
        try:
            result = app.scrape(detail_url, formats=[json_fmt], headers=_DE_HEADERS)
        except TypeError:
            result = app.scrape(detail_url, formats=[json_fmt])
        return _normalise(result)
    except Exception as e:
        print(f"    [warn] detail scrape failed ({detail_url[:60]}): {e}", flush=True)
        return {}


def _enrich_events(app, events: list[dict], venue: dict) -> list[dict]:
    """
    For each event that has a detail_url but no program, scrape the detail
    page to fill in program, performers, conductor (and overwrite price/hall
    if listing didn't have them).
    """
    enriched = []
    for i, ev in enumerate(events):
        detail_url = ev.get("detail_url")
        needs_enrich = not ev.get("program") and detail_url
        if needs_enrich:
            print(f"    [{i+1}/{len(events)}] enriching: {ev.get('title','')[:55]}", flush=True)
            detail = _scrape_one_detail(app, detail_url, venue)
            if detail:
                # Merge: detail wins for program/performers/conductor; listing wins for date/time
                if detail.get("program"):
                    ev["program"] = detail["program"]
                if detail.get("performers"):
                    ev["performers"] = detail["performers"]
                if detail.get("conductor") and not ev.get("conductor"):
                    ev["conductor"] = detail["conductor"]
                if detail.get("price") and not ev.get("price"):
                    ev["price"] = detail["price"]
                if detail.get("venue_hall") and not ev.get("venue_hall"):
                    ev["venue_hall"] = detail["venue_hall"]
                if detail.get("duration_min"):
                    ev["duration_min"] = detail["duration_min"]
                if detail.get("title") and not ev.get("title"):
                    ev["title"] = detail["title"]
                if detail.get("date") and not ev.get("date"):
                    ev["date"] = detail["date"]
                if detail.get("time") and not ev.get("time"):
                    ev["time"] = detail["time"]
        enriched.append(ev)
    return enriched


# ── Main per-venue scrape ─────────────────────────────────────────────────────

def _is_future(event_date: str | None) -> bool:
    if not event_date:
        return True  # keep events with missing dates (can't filter)
    try:
        return event_date >= _today()
    except Exception:
        return True


def _scrape_one(
    app,
    venue: dict,
    max_events: int,
    enrich: bool = True,
    *,
    skip_map: bool = False,
) -> dict:
    slug = _slug(venue["name"])
    url = venue["url"]
    print(f"\n  [{slug}] {venue['name']} ({url})", flush=True)

    payload: dict = {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "engine": "firecrawl",
    }

    # ── Phase 1a: Listing (date-sorted, top-N) ──
    print(f"    phase 1a: listing scrape ...", flush=True)
    listing = _scrape_listing(app, venue, max_events)
    events_raw = listing.get("events") if isinstance(listing, dict) else None
    total_visible = listing.get("total_events_visible")

    if not events_raw:
        payload["error"] = "no events extracted from listing"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_discovered"] = 0
        return payload

    # Filter past events (prompt guard isn't always reliable)
    events: list[dict] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        if not _is_future(e.get("date")):
            continue
        ev = {
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
        }
        events.append(ev)
    events = events[:max_events]

    print(f"    listing: {len(events)} upcoming events (total_visible={total_visible})", flush=True)

    # ── Phase 1b: Full discovery via map() (1 credit; optional) ──
    loose_urls: list[str] = []
    strict_urls: list[str] = []
    if skip_map:
        print(f"    phase 1b: skipped (--skip-map)", flush=True)
    else:
        print(f"    phase 1b: site map for URL counts ...", flush=True)
        loose_urls, strict_urls = _discover_all_event_urls(app, venue)
    total_discovered_loose = len(loose_urls) if loose_urls else total_visible
    total_discovered_strict = len(strict_urls) if strict_urls else total_visible

    # ── Phase 2: Enrich with detail pages ──
    if enrich and events:
        print(f"    phase 2: enriching {len(events)} events with detail pages ...", flush=True)
        events = _enrich_events(app, events, venue)

    payload["events"] = events
    payload["total_events"] = len(events)
    payload["total_events_visible"] = total_visible
    # Loose count (legacy field name — often inflated by map()).
    payload["total_events_discovered"] = total_discovered_loose
    payload["total_events_discovered_strict"] = total_discovered_strict
    return payload


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(
    slugs_filter: list[str] | None,
    max_events: int,
    enrich: bool,
    skip_map: bool = False,
) -> None:
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

    ClientCls = getattr(_fc_module, "Firecrawl", None) or getattr(_fc_module, "FirecrawlApp", None)
    if ClientCls is None:
        print("ERROR: neither Firecrawl nor FirecrawlApp found in firecrawl package",
              file=sys.stderr)
        sys.exit(1)

    app = ClientCls(api_key=api_key)
    print(f"firecrawl-py version: {getattr(_fc_module, '__version__', 'unknown')}")
    print(f"Client class: {ClientCls.__name__}")

    venues = get_venues_by_tier(1)
    if slugs_filter:
        venues = [v for v in venues if _slug(v["name"]) in slugs_filter]

    if not venues:
        print("No venues matched the filter.", file=sys.stderr)
        sys.exit(1)

    map_note = "no map" if skip_map else "map"
    mode = (
        f"full (listing + {map_note} + detail pages)"
        if enrich
        else f"listing only (--skip-enrich, {map_note})"
    )
    print(f"\nFire crawl scraping {len(venues)} Tier-1 venue(s), max {max_events} events. Mode: {mode}\n")

    n_ok = n_fail = 0
    for venue in venues:
        result = _scrape_one(app, venue, max_events, enrich=enrich, skip_map=skip_map)
        out_path = BASE / f"firecrawl_{result['slug']}_events.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

        if result.get("error"):
            n_fail += 1
            print(f"    FAIL: {result['error']}")
        else:
            n_ok += 1
            disc_loose = result.get("total_events_discovered")
            disc_strict = result.get("total_events_discovered_strict")
            disc = disc_strict if disc_strict is not None else disc_loose
            if disc_strict is not None and disc_loose is not None and disc_loose != disc_strict:
                disc = f"{disc_strict} strict ({disc_loose} loose)"
            prog = sum(1 for e in result["events"] if e.get("program"))
            print(
                f"    OK: {result['total_events']} events enriched "
                f"({prog}/{result['total_events']} with program), "
                f"discovered={disc} → {out_path.name}"
            )

    print(f"\nDone. ok={n_ok}  fail={n_fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="SLUG",
                        help="Run only on these venue slugs")
    parser.add_argument("--max-events", type=int, default=5,
                        help="Events to enrich with full data (default 5)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip detail-page enrichment (listing only, fewer credits)")
    parser.add_argument(
        "--skip-map",
        action="store_true",
        help="Skip Firecrawl map() (saves 1 credit/venue; no discovered URL counts)",
    )
    args = parser.parse_args()
    main(args.only, args.max_events, enrich=not args.skip_enrich, skip_map=args.skip_map)
