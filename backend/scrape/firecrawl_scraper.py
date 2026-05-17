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
from backend.scrape import jsonld_scout, raw_store


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


def _extract_markdown(result) -> str:
    """Pull raw markdown out of a Firecrawl response (best effort)."""
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        return ""
    md = result.get("markdown") or (result.get("data") or {}).get("markdown")
    return md or ""


def _extract_html(result) -> str:
    """Pull raw HTML out of a Firecrawl response (best effort).

    Firecrawl renders the page via Playwright before returning, so this HTML
    has the full post-JS DOM — JSON-LD blocks that the server injects on load
    are visible here even when a direct requests.get() gets 403.
    """
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        return ""
    html = (result.get("html")
            or result.get("rawHtml")
            or (result.get("data") or {}).get("html")
            or (result.get("data") or {}).get("rawHtml"))
    return html or ""


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
        f"Today's date is {today}.\n\n"
        "IMPORTANT — the page may contain a long archive of past events shown\n"
        f"FIRST in chronological order (older months at the top). You MUST scan\n"
        f"forward past every entry dated before {today} and only START extracting\n"
        f"once you reach an event dated on or after {today}.\n"
        f"Skip any event with date < {today}, even if it appears at the top.\n"
        "Also skip canceled events (German 'Abgesagt:' / English 'Cancelled:') —\n"
        "do not return them at all.\n\n"
        "TASK (two parts):\n"
        f"1. Count ALL upcoming concerts (date >= {today}) on this fully-scrolled\n"
        "   page. Write the total into 'total_events_visible'.\n"
        f"2. Return the next {max_events} upcoming concerts (sorted by date,\n"
        "   nearest future first) in the 'events' array with COMPLETE data.\n\n"
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


# ── Per-venue overrides ───────────────────────────────────────────────────────
# Sites with cookie walls, click-to-load buttons or heavy SPAs need bespoke
# action sequences. Keys are venue slugs; values override _scroll_actions().
# Reasonable defaults are used for any slug not listed here.
def _click_load_more(times: int, selectors: list[str], wait_ms: int = 1500) -> list[dict]:
    """Try each selector once per round, scrolling between rounds."""
    actions: list[dict] = []
    for _ in range(times):
        actions.append({"type": "scroll", "direction": "down", "amount": 3000})
        actions.append({"type": "wait", "milliseconds": 500})
        for sel in selectors:
            actions.append({"type": "click", "selector": sel})
            actions.append({"type": "wait", "milliseconds": wait_ms})
    return actions


_FIRECRAWL_MAX_ACTIONS = 50  # Hard limit enforced by the Firecrawl API.


def _bp_actions() -> list[dict]:
    # Cookie wall (CMP) then heavy scroll on the SPA infinite-scroll calendar.
    # Budget: 2 cookie clicks + 2 waits + 22 scrolls + 22 waits = 48 ≤ 50.
    return [
        {"type": "wait", "milliseconds": 2500},
        {"type": "click", "selector": "#onetrust-accept-btn-handler"},
        {"type": "click", "selector": "button:has-text('Alle akzeptieren')"},
        {"type": "wait", "milliseconds": 1500},
        *_scroll_actions(n=22, amount=4000, wait_ms=1200),
    ]


def _gewandhaus_actions() -> list[dict]:
    # Homepage shows 5 teasers; "Weitere Veranstaltungen laden" button loads more.
    # Earlier 11-round version regressed from 5 → 3 events (aggressive clicks
    # on non-matching selectors destabilised the page). Lean version: scroll,
    # accept cookies, then click-load 5 rounds with scroll in between.
    # Budget: 2 + 5 × (1 scroll + 1 wait + 2 clicks + 2 waits) = 32 ≤ 50.
    selectors = [
        "button:has-text('Weitere Veranstaltungen')",
        "a:has-text('Weitere Veranstaltungen')",
    ]
    actions: list[dict] = [
        {"type": "wait", "milliseconds": 1500},
        {"type": "click", "selector": "button:has-text('Akzeptieren')"},
    ]
    for _ in range(5):
        actions.append({"type": "scroll", "direction": "down", "amount": 2500})
        actions.append({"type": "wait", "milliseconds": 800})
        for sel in selectors:
            actions.append({"type": "click", "selector": sel})
            actions.append({"type": "wait", "milliseconds": 1200})
    return actions


VENUE_OVERRIDES: dict[str, dict] = {
    "berliner_philharmonie": {
        "actions": _bp_actions,
        "wait_for_listing_count": 20,
    },
    "gewandhaus_leipzig": {
        "actions": _gewandhaus_actions,
        "wait_for_listing_count": 20,
    },
}


def _venue_actions(slug: str) -> list[dict]:
    override = VENUE_OVERRIDES.get(slug)
    if override and callable(override.get("actions")):
        actions = override["actions"]()
    else:
        actions = _scroll_actions()
    # Firecrawl rejects requests with > 50 actions outright.
    if len(actions) > _FIRECRAWL_MAX_ACTIONS:
        actions = actions[:_FIRECRAWL_MAX_ACTIONS]
    return actions


# ── Hallucination guard ───────────────────────────────────────────────────────
# Some sites refuse to render for headless browsers (BP was returning 5 fake
# English-titled concerts with round €5-step prices). Detect obvious LLM
# fabrications BEFORE we overwrite a good JSON with garbage.

_ENGLISH_TELLS = (
    "'s ",                 # "Beethoven's Ninth"
    " symphony",
    " concerto",
    " requiem ",
    " the nutcracker",
    " christmas ",
    " easter ",
)

_GERMAN_TELLS = ("symphonie", "sinfonie", "konzert", "messe", "oper",
                 "kammerkonzert", "philharmoniker", "abend", "uhr")


def _check_hallucination(events: list[dict], venue: dict) -> str | None:
    """Return a reason string if events look fabricated, else None.

    Heuristics target the specific failure mode we've seen: a German venue
    returning a handful of english-titled, round-priced, evenly-spaced events.
    """
    if not events:
        return None

    titles = " ".join((e.get("title") or "").lower() for e in events)
    english_hits = sum(1 for t in _ENGLISH_TELLS if t in titles)
    german_hits = sum(1 for t in _GERMAN_TELLS if t in titles)
    if english_hits >= 2 and german_hits == 0:
        return f"english titles on a German venue ({english_hits} tells, 0 German)"

    # Round-€5 "ab €NN" prices for every event = templated fabrication
    prices = [(e.get("price") or "").strip() for e in events]
    ab_round = sum(
        1 for p in prices
        if re.fullmatch(r"ab\s*€\s*\d{2,3}", p)
        and int(re.search(r"\d+", p).group()) % 5 == 0
    )
    if len(events) >= 4 and ab_round >= len(events) - 1:
        return f"{ab_round}/{len(events)} prices are 'ab €N0' (round-5) — templated"

    return None


def _scrape_listing(app, venue: dict, max_events: int) -> dict:
    """
    Scrape the listing page with scroll actions.
    Returns the raw Firecrawl extraction dict, augmented with `_raw_html`
    (string) — used downstream for the JSON-LD post-pass — or {}.
    Also persists the raw markdown to backend/raw/<slug>/ for reprocessing.
    """
    slug = _slug(venue["name"])
    prompt = _build_listing_prompt(venue, max_events)
    schema = EventList.model_json_schema()
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    # html is requested alongside json/markdown — Firecrawl charges by the
    # most expensive format (json), so the extra html costs 0 credits.
    formats = [json_fmt, "markdown", "html"]
    actions = _venue_actions(slug)

    last_err: Exception | None = None

    def _attempt(extra: dict) -> dict:
        result = app.scrape(venue["url"], formats=formats, **extra)
        md = _extract_markdown(result)
        if md:
            try:
                raw_store.save_markdown(slug, venue["url"], md)
            except Exception as exc:
                print(f"    [warn] could not save raw markdown: {exc}", flush=True)
        extracted = _normalise(result) or {}
        html = _extract_html(result)
        if html:
            extracted = dict(extracted)
            extracted["_raw_html"] = html
        return extracted

    # Try with scroll actions first, then without (some sites reject action requests)
    for with_actions in (True, False):
        extra: dict = {"headers": _DE_HEADERS}
        if with_actions:
            extra["actions"] = actions
        try:
            extracted = _attempt(extra)
            if extracted.get("events") or extracted.get("_raw_html"):
                return extracted
        except TypeError as e:
            # unexpected kwarg (e.g. SDK doesn't accept headers) — retry without it
            if "headers" in str(e):
                extra.pop("headers", None)
                try:
                    extracted = _attempt(extra)
                    if extracted.get("events") or extracted.get("_raw_html"):
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


_CANCELED_PREFIXES = ("abgesagt:", "abgesagt ", "abgesagt-",
                      "cancelled:", "canceled:", "entfällt:", "entfällt ")


def _is_canceled(title: str) -> bool:
    t = (title or "").strip().lower()
    return any(t.startswith(p) for p in _CANCELED_PREFIXES)


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

    events_raw: list[dict] | None = None
    total_visible: int | None = None
    source: str = "firecrawl_listing"

    # ── Phase 0: JSON-LD scout (free, deterministic) ──
    print(f"    phase 0: JSON-LD scout ...", flush=True)
    scout_events = jsonld_scout.scout(url)
    if scout_events:
        future = [e for e in scout_events if _is_future(e.get("date"))]
        print(
            f"    JSON-LD: {len(scout_events)} events ({len(future)} upcoming)",
            flush=True,
        )
        if future:
            future.sort(key=lambda e: (e.get("date") or "9999"))
            events_raw = future
            total_visible = len(future)
            source = "jsonld"
    else:
        print(f"    JSON-LD: no Event objects found", flush=True)

    # ── Phase 1a: Firecrawl listing fallback ──
    # Ask for more than max_events so the post-filter for past events doesn't
    # leave us empty when the listing starts with an archive (e.g. Isarphilharmonie
    # lists from Sept 2025 chronologically — first 5 would all be past).
    listing: dict = {}
    if events_raw is None:
        listing_target = max(max_events * 4, 20)
        print(f"    phase 1a: listing scrape (firecrawl, target={listing_target}) ...", flush=True)
        listing = _scrape_listing(app, venue, listing_target) or {}
        events_raw = listing.get("events") if isinstance(listing, dict) else None
        total_visible = listing.get("total_events_visible") if isinstance(listing, dict) else None

    # ── Phase 0b: JSON-LD from Firecrawl-rendered HTML ──
    # Firecrawl bypasses the datacenter-IP block that Phase 0 (direct HTTP) hits.
    # If the rendered HTML carries JSON-LD Event nodes, prefer those — they're
    # deterministic, complete, and avoid LLM hallucination.
    if source != "jsonld" and isinstance(listing, dict) and listing.get("_raw_html"):
        ld_events = jsonld_scout.extract_from_html(
            listing["_raw_html"], url, verbose=True, log_prefix="jsonld-fc"
        )
        if ld_events:
            ld_future = [e for e in ld_events if _is_future(e.get("date"))]
            print(
                f"    JSON-LD (firecrawl-html): {len(ld_events)} events "
                f"({len(ld_future)} upcoming)",
                flush=True,
            )
            if ld_future:
                ld_future.sort(key=lambda e: (e.get("date") or "9999"))
                events_raw = ld_future
                total_visible = len(ld_future)
                source = "jsonld_firecrawl_html"

    if not events_raw:
        payload["error"] = "no events extracted from listing"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_discovered"] = 0
        payload["source"] = source
        return payload

    # Filter past + canceled events (prompt guard isn't always reliable)
    events: list[dict] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        if not _is_future(e.get("date")):
            continue
        title = (e.get("title") or "").strip()
        if _is_canceled(title):
            continue
        ev = {
            "date": e.get("date"),
            "time": e.get("time"),
            "title": title,
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

    # Hallucination guard — refuse to save fabricated output
    halluc = _check_hallucination(events, venue)
    if halluc:
        print(f"    HALLUCINATION_SUSPECTED: {halluc}", flush=True)
        payload["error"] = f"hallucination suspected: {halluc}"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_visible"] = total_visible
        payload["total_events_discovered"] = 0
        return payload

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
    payload["source"] = source
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

        if result.get("error"):
            n_fail += 1
            print(f"    FAIL: {result['error']}")
            # Don't overwrite a previously-good JSON with an error payload.
            if out_path.exists():
                print(f"    (keeping existing {out_path.name} untouched)")
            else:
                out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            n_ok += 1
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
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
