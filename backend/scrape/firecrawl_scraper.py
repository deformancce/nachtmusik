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
from backend.scrape.classify import is_classical_event


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


class EventStub(BaseModel):
    """Minimal event schema for discovery mode — only fields we can
    reliably extract from a listing page without context bloat. This
    schema is ~5× smaller than `Event`, which lets the LLM return many
    more events per scrape (~100-200 vs. ~30)."""
    date: str = Field(..., description="ISO date YYYY-MM-DD")
    time: Optional[str] = Field(None, description="HH:MM in 24-hour format if visible")
    title: str = Field(..., description="Concert title (not the ticket button)")
    detail_url: Optional[str] = Field(None, description="Absolute URL of the event detail page")
    venue_hall: Optional[str] = Field(None, description="Hall name if visible (e.g. 'Großer Saal')")


class EventStubList(BaseModel):
    total_events_visible: Optional[int] = Field(
        None,
        description="Total number of upcoming concerts on this fully-scrolled page.",
    )
    events: list[EventStub]


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
        "Skip any event labeled 'Gastkonzert' (guest tour) or showing a different city/venue "
        "as location. Cross-promotion of other venues must also be skipped.\n"
        "- NEVER invent dates, URLs, program works, or performer names. Only use what is "
        "explicitly written on the page. If a field is not shown, leave it null/empty.\n"
        "  A title like 'Die Prinzen Symphonisch' is a pop show — do NOT invent a Mahler symphony.\n"
        "- If the page shows the date only as a day-of-week + German date (e.g. 'Sa. 23.05.2026'), "
        "convert that exactly to YYYY-MM-DD. Do NOT guess or infer dates from URL slugs.\n"
        "- Skip non-concert entries: theater, dance, lectures, navigation items, season-pass upsells.\n"
        "- detail_url must be an absolute URL (start with https://). If you cannot find one, use null."
    )


def _build_stub_listing_prompt(venue: dict) -> str:
    """Discovery-mode prompt: find ALL events on the listing page with minimal
    fields (date, title, URL, hall). Designed for maximum recall — the LLM can
    return 100+ events per scrape because each entry is tiny (no program/performers/
    conductor/price)."""
    today = _today()
    return (
        f"This is the concert listing page of {venue['name']} in {venue['city']}, Germany.\n"
        f"Today's date is {today}.\n\n"
        "TASK: Find EVERY upcoming concert on this fully-scrolled page and "
        "return ALL of them. There may be 50, 100, 200+ events — return as "
        "many as you can find. Do NOT cap or summarize.\n\n"
        f"Skip events with date < {today} (past concerts).\n"
        "Skip canceled events (German 'Abgesagt:' / English 'Cancelled:').\n"
        f"Only events physically AT {venue['name']} in {venue['city']} — skip "
        "guest tours to other cities ('Gastkonzert') and cross-promotion of other venues.\n"
        "Skip non-concert entries: theater plays, ballet, lectures, navigation items, season-pass upsells.\n\n"
        "For each event return ONLY these fields:\n"
        "  date (YYYY-MM-DD, REQUIRED, convert from German format like 'Sa. 23.05.2026' exactly)\n"
        "  time (HH:MM 24h, optional)\n"
        "  title (REQUIRED — the concert name, NOT a ticket button)\n"
        "  detail_url (absolute URL of the event's own page, starting with https://)\n"
        "  venue_hall (e.g. 'Großer Saal', optional)\n\n"
        "Do NOT include program, performers, conductor, or price — those will be "
        "fetched separately. Focus on RECALL: every distinct upcoming concert."
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
        "NEVER invent data. Only extract what is explicitly written on this page.\n"
        f"If no specific date is stated on this page, set date to null — never guess "
        f"or use today's date ({today}) as a fallback. A null date is correct; a wrong "
        f"date is not.\n"
        f"If this page is a tour overview listing concerts at multiple venues (not solely "
        f"at {venue['name']} in {venue['city']}), extract the data for the {venue['city']} "
        f"performance only. If no {venue['city']} performance is listed, set date to null."
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


# ── Universal cookie + load-more JS ──────────────────────────────────────────
# Firecrawl's `click` action is fail-fast: a missing selector kills the whole
# action sequence ("Error in action N: Element not found"). Sites are also
# inconsistent — cookie banners come and go, button text changes. One robust
# executeJavascript action is more reliable than 40+ brittle clicks.
#
# The script:
#  1. Dismisses any visible cookie/consent banner by clicking the first button
#     whose text matches accept-keywords. Never throws when none is found.
#  2. Loops up to N times: scroll-to-bottom, look for a load-more button,
#     click it, wait, count events. Stop when no more progress.
#  3. Returns a short summary string that Firecrawl surfaces in logs.
_COOKIE_AND_LOAD_MORE_JS_TPL = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const log = [];

  // (1) Dismiss cookie / consent banner (best-effort, never throws)
  const cookieRe = /^(alle\s+akzeptieren|akzeptieren|alle\s+cookies\s+akzeptieren|einverstanden|zustimmen|alles\s+erlauben|alle\s+aktivieren|accept(?:\s+all)?|agree|got\s+it)$/i;
  let cookieClicked = false;
  for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"]')) {
    const t = (el.textContent || el.value || '').trim();
    if (!t || t.length > 60) continue;
    if (cookieRe.test(t)) {
      try {
        el.click();
        cookieClicked = true;
        log.push('cookie:' + t);
        break;
      } catch (e) {}
    }
  }
  if (!cookieClicked) log.push('cookie:none');
  await sleep(800);

  // (2) Repeatedly click any visible load-more button, scrolling between
  const loadMoreRe = /weitere\s+veranstaltungen|weitere\s+(termine|konzerte)|mehr\s+(laden|anzeigen|veranstaltungen)|nächste\s+seite|load\s+more|show\s+more|more\s+events/i;
  const MAX_ROUNDS = __MAX_ROUNDS__;
  let lastHeight = 0;
  let rounds = 0;
  let buttonClicks = 0;
  for (let i = 0; i < MAX_ROUNDS; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(700);
    let clicked = null;
    for (const el of document.querySelectorAll('a, button, [role="button"]')) {
      if (el.offsetParent === null) continue;  // not visible
      const t = (el.textContent || '').trim();
      if (!t || t.length > 80) continue;
      if (loadMoreRe.test(t)) {
        try {
          el.scrollIntoView({block: 'center'});
          el.click();
          clicked = t;
          buttonClicks++;
          break;
        } catch (e) {}
      }
    }
    if (clicked) {
      await sleep(1500);
    } else {
      // No load-more button: keep scrolling for infinite-scroll pages until
      // page height stops growing.
      await sleep(600);
      const h = document.body.scrollHeight;
      if (h === lastHeight) break;
      lastHeight = h;
    }
    rounds = i + 1;
  }
  log.push('rounds:' + rounds);
  log.push('clicks:' + buttonClicks);
  log.push('height:' + document.body.scrollHeight);
  return log.join(' | ');
}
"""


def _cookie_and_load_more_actions(max_rounds: int = 25, settle_ms: int = 1500) -> list[dict]:
    """3-action sequence: initial wait, JS cookie+load-more loop, final settle.
    Replaces brittle click-selector chains with one fail-safe executeJavascript."""
    script = _COOKIE_AND_LOAD_MORE_JS_TPL.replace("__MAX_ROUNDS__", str(max_rounds))
    return [
        {"type": "wait", "milliseconds": 2000},
        {"type": "executeJavascript", "script": script},
        {"type": "wait", "milliseconds": settle_ms},
    ]


def _bp_actions() -> list[dict]:
    # SPA infinite-scroll calendar; previously failed on Firecrawl's brittle
    # selector clicks ("Error in action 1: Element not found"). JS handles
    # OneTrust + infinite scroll robustly.
    return _cookie_and_load_more_actions(max_rounds=30, settle_ms=2000)


def _gewandhaus_actions() -> list[dict]:
    # Homepage shows 5 teasers; "Weitere Veranstaltungen laden" loads more.
    # The old dedicated scraper does up to 60 clicks per category.
    return _cookie_and_load_more_actions(max_rounds=30, settle_ms=2000)


# mphil.de calendar renders all events from Sept 2025 → future (13k+ line markdown).
# Clicking the current-month tab narrows the page to ~2 months of events and
# prevents LLM date-confusion / hallucination caused by the huge archive.
_MPHIL_MONTH_NAV_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const log = [];

  // (1) Dismiss cookie banner
  const cookieRe = /^(alle\s+akzeptieren|akzeptieren|alle\s+cookies\s+akzeptieren|einverstanden|zustimmen|alles\s+erlauben|alle\s+aktivieren|accept(?:\s+all)?|agree|got\s+it)$/i;
  for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"]')) {
    const t = (el.textContent || el.value || '').trim();
    if (!t || t.length > 60) continue;
    if (cookieRe.test(t)) {
      try { el.click(); log.push('cookie:' + t); break; } catch (e) {}
    }
  }
  await sleep(900);

  // (2) Click the current-month navigation tab (German month name + year).
  // mphil.de renders "Mai 2026", "Juni 2026", … as clickable month tabs.
  const DE_MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli',
                     'August','September','Oktober','November','Dezember'];
  const now = new Date();
  const target = DE_MONTHS[now.getMonth()] + ' ' + now.getFullYear();  // nbsp variant
  const target2 = DE_MONTHS[now.getMonth()] + ' ' + now.getFullYear();      // regular space
  let monthClicked = false;
  for (const el of document.querySelectorAll('a, button, li, span, [role="tab"], [role="option"]')) {
    const t = (el.textContent || '').trim();
    if (t === target || t === target2 || t.replace(/\s+/g,' ') === target2) {
      try {
        el.scrollIntoView({block: 'center'});
        el.click();
        monthClicked = true;
        log.push('month:' + t);
        break;
      } catch (e) {}
    }
  }
  if (!monthClicked) log.push('month:not-found');
  await sleep(1200);

  // (3) Scroll to load any lazy content within the month view
  let lastH = 0;
  for (let i = 0; i < 8; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(500);
    const h = document.body.scrollHeight;
    if (h === lastH) break;
    lastH = h;
  }
  log.push('height:' + document.body.scrollHeight);
  return log.join(' | ');
}
"""


def _isarphi_actions() -> list[dict]:
    # mphil.de shows the full archive (13k+ lines). Click the current month tab
    # first to reduce the page to only upcoming events and avoid LLM confusion.
    return [
        {"type": "wait", "milliseconds": 2000},
        {"type": "executeJavascript", "script": _MPHIL_MONTH_NAV_JS},
        {"type": "wait", "milliseconds": 1500},
    ]


VENUE_OVERRIDES: dict[str, dict] = {
    "berliner_philharmonie": {
        "actions": _bp_actions,
        "wait_for_listing_count": 20,
    },
    "elbphilharmonie_hamburg": {
        # Heavy cookie wall ("Alle akzeptieren") blocks all content without JS dismissal.
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=20, settle_ms=2000),
    },
    "gewandhaus_leipzig": {
        "actions": _gewandhaus_actions,
        "wait_for_listing_count": 20,
    },
    "isarphilharmonie_muenchen": {
        # mphil.de calendar is 13k+ lines; navigate to current month to avoid hallucination.
        "actions": _isarphi_actions,
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


def _scrape_listing_with_schema(
    app, venue: dict, schema: dict, prompt: str
) -> dict:
    """
    Scrape the listing page with scroll actions, extracting per the given
    schema/prompt. Returns the raw Firecrawl extraction dict, augmented
    with `_raw_html` (string) for downstream JSON-LD post-pass — or {}.
    Also persists raw markdown to backend/raw/<slug>/.
    """
    slug = _slug(venue["name"])
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


def _scrape_listing(app, venue: dict, max_events: int) -> dict:
    """Full-schema listing scrape: returns events with date+title+program+performers+...
    Used when the goal is to get full data for a small number of events."""
    return _scrape_listing_with_schema(
        app, venue,
        schema=EventList.model_json_schema(),
        prompt=_build_listing_prompt(venue, max_events),
    )


def _scrape_listing_stub(app, venue: dict) -> dict:
    """Stub-schema listing scrape: returns events with ONLY date+title+url+hall.
    Used for discovery mode — the LLM can return 100-200 events in one scrape
    because each event is ~5× smaller than the full schema."""
    return _scrape_listing_with_schema(
        app, venue,
        schema=EventStubList.model_json_schema(),
        prompt=_build_stub_listing_prompt(venue),
    )


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


def _is_valid_url(url) -> bool:
    """True if `url` is a usable HTTP(S) URL — not None, '', 'null', or junk.
    The LLM occasionally returns the literal string 'null' instead of JSON null;
    passing that to Firecrawl wastes a credit on a guaranteed 400 response."""
    if not isinstance(url, str):
        return False
    u = url.strip().lower()
    if u in ("", "null", "none", "n/a", "undefined"):
        return False
    return u.startswith(("http://", "https://"))


def _enrich_events(app, events: list[dict], venue: dict) -> list[dict]:
    """
    For each event that has a detail_url but no program, scrape the detail
    page to fill in program, performers, conductor (and overwrite price/hall
    if listing didn't have them).
    """
    enriched = []
    for i, ev in enumerate(events):
        detail_url = ev.get("detail_url")
        needs_enrich = not ev.get("program") and _is_valid_url(detail_url)
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


def _detail_to_event(detail: dict, url: str, venue: dict) -> dict | None:
    """Build a full event dict from a detail-page scrape result.
    Returns None if the detail is unusable (missing date/title)."""
    if not isinstance(detail, dict):
        return None
    title = (detail.get("title") or "").strip()
    date = detail.get("date")
    if not title or not date:
        return None
    ev: dict = {
        "date": date,
        "time": detail.get("time"),
        "title": title,
        "venue_hall": detail.get("venue_hall"),
        "program": detail.get("program") or [],
        "performers": detail.get("performers") or [],
        "conductor": detail.get("conductor"),
        "price": detail.get("price"),
        "detail_url": url,
        "venue": venue["name"],
        "city": venue["city"],
    }
    if detail.get("duration_min"):
        ev["duration_min"] = detail["duration_min"]
    return ev


def _expand_via_map(
    app,
    venue: dict,
    listing_events: list[dict],
    target: int,
) -> tuple[list[dict], dict]:
    """
    Use Firecrawl map() to discover event URLs NOT already in `listing_events`,
    detail-scrape each one, and return the NEW future events.

    Stops scraping when `target` total events would be reached
    (i.e. len(listing_events) + len(new_events) >= target) or when the map
    budget is exhausted.

    Returns:
      (new_events, stats) where new_events is the list to APPEND to listing_events.
    """
    stats = {"map_urls": 0, "new_urls": 0, "scraped": 0, "past": 0, "invalid": 0, "canceled": 0}
    needed = target - len(listing_events)
    if needed <= 0:
        return [], stats

    loose_urls, strict_urls = _discover_all_event_urls(app, venue)
    map_urls = strict_urls if strict_urls else loose_urls
    stats["map_urls"] = len(map_urls)
    if not map_urls:
        print(f"    [map-expand] map() returned no event URLs", flush=True)
        return [], stats

    known = {e.get("detail_url") for e in listing_events if e.get("detail_url")}
    new_urls = [u for u in map_urls if u and u not in known]
    stats["new_urls"] = len(new_urls)
    print(
        f"    [map-expand] {len(map_urls)} event URLs from map "
        f"({len(new_urls)} new beyond listing); need {needed} more events",
        flush=True,
    )
    if not new_urls:
        return [], stats

    # Budget: allow up to 3× overhead for past/invalid scrapes.
    budget = min(len(new_urls), needed * 3)
    new_events: list[dict] = []
    for i, url in enumerate(new_urls[:budget], start=1):
        if len(new_events) >= needed:
            break
        if not _is_valid_url(url):
            stats["invalid"] += 1
            continue
        stats["scraped"] += 1
        print(f"    [map-expand {i}/{budget}] {url[:75]}", flush=True)
        detail = _scrape_one_detail(app, url, venue)
        ev = _detail_to_event(detail, url, venue)
        if ev is None:
            stats["invalid"] += 1
            continue
        if not _is_future(ev["date"]):
            stats["past"] += 1
            continue
        if _is_canceled(ev["title"]):
            stats["canceled"] += 1
            continue
        # Guard: map-discovered pages sometimes have no visible date (tour overviews,
        # festival landing pages) — prompt now instructs LLM to return null, which makes
        # _detail_to_event return None above. Belt-and-suspenders: if date == today and
        # title contains typical tour/overview keywords, skip rather than propagate noise.
        _title_lower = (ev.get("title") or "").lower()
        if ev["date"] == _today() and any(
            kw in _title_lower for kw in ("tournee", " tour", "festival-tournee")
        ):
            stats["suspicious_date"] = stats.get("suspicious_date", 0) + 1
            continue
        new_events.append(ev)

    print(
        f"    [map-expand] result: +{len(new_events)} new events "
        f"(scraped={stats['scraped']} past={stats['past']} "
        f"invalid={stats['invalid']} canceled={stats['canceled']} "
        f"suspicious_date={stats.get('suspicious_date', 0)})",
        flush=True,
    )
    return new_events, stats


def _scrape_one_discover(
    app,
    venue: dict,
    enrich_count: int = 10,
) -> dict:
    """
    Discovery mode: get as many events as possible from the listing (light
    schema), then fully enrich `enrich_count` random ones with program data.

    Cost per venue: 1 listing scrape + enrich_count detail scrapes
    (~11 credits at default enrich_count=10).

    Returns the same payload shape as _scrape_one.
    """
    import random
    slug = _slug(venue["name"])
    url = venue["url"]
    print(f"\n  [{slug}] {venue['name']} ({url}) — DISCOVERY mode", flush=True)

    payload: dict = {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "engine": "firecrawl",
        "mode": "discover",
    }

    # ── Phase 0: JSON-LD scout (free) ──
    print(f"    phase 0: JSON-LD scout ...", flush=True)
    scout_events = jsonld_scout.scout(url)
    events_raw: list[dict] = []
    total_visible: int | None = None
    source = "firecrawl_listing_stub"
    if scout_events:
        future = [e for e in scout_events if _is_future(e.get("date"))]
        if future:
            print(f"    JSON-LD: {len(future)} upcoming events found", flush=True)
            events_raw = future
            total_visible = len(future)
            source = "jsonld"

    # ── Phase 1: Stub listing scrape (1 credit, light schema) ──
    if not events_raw:
        print(f"    phase 1: stub listing scrape (find ALL events) ...", flush=True)
        listing = _scrape_listing_stub(app, venue) or {}
        events_raw = listing.get("events") or []
        total_visible = listing.get("total_events_visible") or len(events_raw)
        # Phase 0b: JSON-LD from Firecrawl-rendered HTML, if listing returned little
        if isinstance(listing, dict) and listing.get("_raw_html"):
            ld_events = jsonld_scout.extract_from_html(
                listing["_raw_html"], url, verbose=True, log_prefix="jsonld-fc"
            )
            if ld_events:
                ld_future = [e for e in ld_events if _is_future(e.get("date"))]
                if len(ld_future) > len(events_raw):
                    print(
                        f"    JSON-LD (firecrawl-html): {len(ld_future)} upcoming events "
                        f"(prefer over {len(events_raw)} from listing LLM)",
                        flush=True,
                    )
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

    # Filter past + canceled events
    events: list[dict] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        if not _is_future(e.get("date")):
            continue
        title = (e.get("title") or "").strip()
        if _is_canceled(title) or not title:
            continue
        ev = {
            "date": e.get("date"),
            "time": e.get("time"),
            "title": title,
            "venue_hall": e.get("venue_hall"),
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": e.get("detail_url"),
            "venue": venue["name"],
            "city": venue["city"],
        }
        events.append(ev)

    events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))
    print(f"    discovered {len(events)} future events (total_visible={total_visible})", flush=True)

    # ── Phase 2: Random enrichment ──
    enrichable = [i for i, e in enumerate(events) if _is_valid_url(e.get("detail_url"))]
    if enrichable and enrich_count > 0:
        # Pick `enrich_count` random indices (or all if fewer).
        k = min(enrich_count, len(enrichable))
        chosen = sorted(random.sample(enrichable, k))
        print(
            f"    phase 2: enriching {k} random events of {len(enrichable)} "
            f"with valid URLs ...",
            flush=True,
        )
        for n, idx in enumerate(chosen, start=1):
            ev = events[idx]
            print(f"    [{n}/{k}] {ev['title'][:55]} ({ev.get('date','?')})", flush=True)
            detail = _scrape_one_detail(app, ev["detail_url"], venue)
            if not detail:
                continue
            if detail.get("program"):
                ev["program"] = detail["program"]
            if detail.get("performers"):
                ev["performers"] = detail["performers"]
            if detail.get("conductor"):
                ev["conductor"] = detail["conductor"]
            if detail.get("price"):
                ev["price"] = detail["price"]
            if detail.get("venue_hall") and not ev.get("venue_hall"):
                ev["venue_hall"] = detail["venue_hall"]
            if detail.get("duration_min"):
                ev["duration_min"] = detail["duration_min"]
            if detail.get("time") and not ev.get("time"):
                ev["time"] = detail["time"]
    else:
        print(f"    phase 2: skipped (no valid URLs to enrich)", flush=True)

    # Classify each event as classical (or jazz/opera/lieder) vs pop/musical/etc.
    for ev in events:
        ev["is_classical"] = is_classical_event(ev)

    payload["events"] = events
    payload["total_events"] = len(events)
    payload["total_events_visible"] = total_visible
    payload["total_events_discovered"] = len(events)
    payload["total_events_enriched"] = sum(1 for e in events if e.get("program"))
    payload["source"] = source
    return payload


def _scrape_one(
    app,
    venue: dict,
    max_events: int,
    enrich: bool = True,
    *,
    skip_map: bool = False,
    expand_via_map: bool = False,
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
        # Cap target at 60 — asking the LLM for 160+ events confuses it on
        # smaller listings (BP returned 24 instead of 63 in one observed run).
        # max_events*2 + 10 buffer for filtered-out past/canceled events.
        listing_target = min(max(max_events * 2 + 10, 20), 60)
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

    # ── Phase 1c: Expand via map() — detail-scrape URLs beyond the listing ──
    # When --expand-via-map is set, run map() to discover ALL event URLs on the
    # domain. For URLs NOT already in the listing, detail-scrape each one to
    # add as a new event. This is how we get from ~30 listing events to
    # max_events (e.g. 100-200) — listing alone can't reach season depth.
    map_stats: dict = {}
    new_from_map: list[dict] = []
    total_discovered_loose: int | None = total_visible
    total_discovered_strict: int | None = total_visible
    if expand_via_map and source not in ("jsonld", "jsonld_firecrawl_html"):
        # Only expand when listing was the source (JSON-LD is already complete).
        new_from_map, map_stats = _expand_via_map(app, venue, events, max_events)
        total_discovered_loose = map_stats.get("map_urls") or total_visible
        total_discovered_strict = map_stats.get("map_urls") or total_visible
    elif not skip_map:
        # Legacy stats-only path (no detail scraping).
        print(f"    phase 1b: site map for URL counts ...", flush=True)
        loose_urls, strict_urls = _discover_all_event_urls(app, venue)
        total_discovered_loose = len(loose_urls) if loose_urls else total_visible
        total_discovered_strict = len(strict_urls) if strict_urls else total_visible
    else:
        print(f"    phase 1b: skipped (--skip-map)", flush=True)

    # ── Phase 2: Enrich listing events with detail pages ──
    # Note: map-discovered events (new_from_map) are already fully populated
    # from detail scrapes, so they don't need enrichment here.
    if enrich and events:
        print(f"    phase 2: enriching {len(events)} listing events with detail pages ...", flush=True)
        events = _enrich_events(app, events, venue)

    # Merge listing + map-discovered, dedupe by detail_url, sort by date.
    if new_from_map:
        seen_urls = {e.get("detail_url") for e in events if e.get("detail_url")}
        for ev in new_from_map:
            if ev.get("detail_url") in seen_urls:
                continue
            events.append(ev)
            seen_urls.add(ev.get("detail_url"))
        events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))
        events = events[:max_events]
        print(f"    merged: {len(events)} total events after map-expand", flush=True)

    # Classify each event as classical (or jazz/opera/lieder) vs pop/musical/etc.
    for ev in events:
        ev["is_classical"] = is_classical_event(ev)

    payload["events"] = events
    payload["total_events"] = len(events)
    payload["total_events_visible"] = total_visible
    # Loose count (legacy field name — often inflated by map()).
    payload["total_events_discovered"] = total_discovered_loose
    payload["total_events_discovered_strict"] = total_discovered_strict
    payload["source"] = source
    if map_stats:
        payload["map_expand_stats"] = map_stats
    return payload


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(
    slugs_filter: list[str] | None,
    max_events: int,
    enrich: bool,
    skip_map: bool = False,
    expand_via_map: bool = False,
    discover_mode: bool = False,
    enrich_count: int = 10,
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

    if discover_mode:
        mode = f"DISCOVER (stub listing + {enrich_count} random enrichments per venue)"
    else:
        if expand_via_map:
            map_note = "expand-via-map"
        elif skip_map:
            map_note = "no map"
        else:
            map_note = "map (stats only)"
        mode = (
            f"full (listing + {map_note} + detail pages)"
            if enrich
            else f"listing only (--skip-enrich, {map_note})"
        )
    print(f"\nFire crawl scraping {len(venues)} Tier-1 venue(s), max {max_events} events. Mode: {mode}\n")

    n_ok = n_fail = 0
    for venue in venues:
        if discover_mode:
            result = _scrape_one_discover(app, venue, enrich_count=enrich_count)
        else:
            result = _scrape_one(
                app, venue, max_events, enrich=enrich,
                skip_map=skip_map, expand_via_map=expand_via_map,
            )
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
    parser.add_argument(
        "--expand-via-map",
        action="store_true",
        help=(
            "After listing+enrichment, use map() to find more event URLs and "
            "detail-scrape each one as a NEW event until --max-events is reached. "
            "Cost per venue: 1 map + up to (max_events × 3) detail scrapes."
        ),
    )
    parser.add_argument(
        "--discover-mode",
        action="store_true",
        help=(
            "DISCOVERY mode: use a light schema (date+title+URL only) to find "
            "ALL events on the listing in ONE scrape (100-200 events possible), "
            "then enrich --enrich-count random ones with full program data. "
            "Coverage > depth. Cost: 1 + enrich_count credits/venue."
        ),
    )
    parser.add_argument(
        "--enrich-count",
        type=int,
        default=10,
        help="Discovery mode: number of random events to fully enrich (default 10).",
    )
    args = parser.parse_args()
    main(
        args.only, args.max_events,
        enrich=not args.skip_enrich,
        skip_map=args.skip_map,
        expand_via_map=args.expand_via_map,
        discover_mode=args.discover_mode,
        enrich_count=args.enrich_count,
    )
