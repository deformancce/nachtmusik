"""
Firecrawl-based scraper for Tier-0 concert venues.

Two-phase approach per venue:
  Phase 1 — Discovery: map() the site to find ALL event URLs (1 credit).
             Also scrape the listing page with scroll actions (1 credit) to
             get the next N events in date order.
  Phase 2 — Enrichment: scrape each event detail page for full data
             (program, performers, conductor, price). (N credits, default 5)

Total credits per venue: ~7 (1 map + 1 listing + 5 details)
Total for all 13 Tier-0 venues: ~91 credits/run

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
import calendar
import time
import unicodedata
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

from pydantic import BaseModel, Field
import requests

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE.parent))
from backend.venues_germany import get_venues_by_tier
from backend.scrape.url_filters import filter_event_urls, is_strict_event_url
from backend.scrape import jsonld_scout, raw_store
from backend.scrape.classify import (
    has_classical_signal,
    has_non_classical_signal,
    is_classical_event,
)


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


def _today_date() -> date:
    return datetime.utcnow().date()


def _end_of_month_after_months(start: date, months: int) -> date:
    month_index = start.month - 1 + max(months, 0)
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _scrape_horizon_date(months: int | None = None) -> str:
    if months is None:
        try:
            months = int(os.getenv("SCRAPE_HORIZON_MONTHS", "6"))
        except ValueError:
            months = 6
    return _end_of_month_after_months(_today_date(), months).isoformat()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    horizon = _scrape_horizon_date()
    return (
        f"This is the concert listing page of {venue['name']} in {venue['city']}, Germany.\n"
        f"Today's date is {today}. The scrape horizon is {horizon}.\n\n"
        "IMPORTANT — the page may contain a long archive of past events shown\n"
        f"FIRST in chronological order (older months at the top). You MUST scan\n"
        f"forward past every entry dated before {today} and only START extracting\n"
        f"once you reach an event dated on or after {today}.\n"
        f"Skip any event with date < {today}, even if it appears at the top.\n"
        f"Stop at the scrape horizon: skip any event with date > {horizon}.\n"
        "Also skip canceled events (German 'Abgesagt:' / English 'Cancelled:') —\n"
        "do not return them at all.\n\n"
        "TASK (two parts):\n"
        f"1. Count ALL upcoming concerts from {today} through {horizon} on this fully-scrolled\n"
        "   page. Write the total into 'total_events_visible'.\n"
        f"2. Return the next {max_events} upcoming concerts in that date window (sorted by date,\n"
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
        "- detail_url must be an absolute URL (start with https://). Look at the href "
        "attribute of links like 'Programmdetails', 'Mehr', 'Details', or the event-title "
        "anchor — those point at the event's own page. If no such anchor exists, use null."
    )


def _build_stub_listing_prompt(venue: dict) -> str:
    """Discovery-mode prompt: find ALL events on the listing page with minimal
    fields (date, title, URL, hall). Designed for maximum recall — the LLM can
    return 100+ events per scrape because each entry is tiny (no program/performers/
    conductor/price)."""
    today = _today()
    horizon = _scrape_horizon_date()
    return (
        f"This is the concert listing page of {venue['name']} in {venue['city']}, Germany.\n"
        f"Today's date is {today}. The scrape horizon is {horizon}.\n\n"
        "TASK: Find EVERY upcoming concert in the scrape window on this fully-scrolled page and "
        "return ALL of them. There may be 50, 100, 200+ events — return as "
        "many as you can find. Do NOT cap or summarize.\n\n"
        f"Skip events with date < {today} (past concerts).\n"
        f"Skip events with date > {horizon} (beyond the scrape horizon).\n"
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
    horizon = _scrape_horizon_date()
    return (
        f"This is an event detail page for a concert at {venue['name']} in {venue['city']}, Germany.\n"
        f"Today is {today}. The scrape horizon is {horizon}.\n"
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
    return _cookie_and_load_more_actions(max_rounds=60, settle_ms=2000)


def _gewandhaus_actions() -> list[dict]:
    # Gewandhaus' load-more button is present in the rendered text, but
    # headless visibility checks can report it as hidden. Click by text without
    # offsetParent gating, mirroring the older Playwright scraper.
    script = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const log = [];
  const cookieRe = /^(alle\s+akzeptieren|akzeptieren|alle\s+cookies\s+akzeptieren|einverstanden|zustimmen|alles\s+erlauben|alle\s+aktivieren|accept(?:\s+all)?|agree|got\s+it)$/i;
  for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"]')) {
    const t = (el.textContent || el.value || '').trim();
    if (t && t.length <= 80 && cookieRe.test(t)) {
      try { el.click(); log.push('cookie:' + t); break; } catch (e) {}
    }
  }
  await sleep(1000);
  const countEvents = () => document.querySelectorAll('[class*="event-teaser"], [id^="event-"], a[href*="/veranstaltung/"]').length;
  let lastCount = countEvents();
  let clicks = 0;
  for (let i = 0; i < 60; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(500);
    let clicked = false;
    for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"]')) {
      const t = (el.textContent || el.value || '').trim();
      if (!/Weitere\s+Veranstaltungen\s+laden/i.test(t)) continue;
      try {
        el.scrollIntoView({block: 'center'});
        el.click();
        clicked = true;
        clicks++;
        break;
      } catch (e) {}
    }
    if (!clicked) break;
    await sleep(1700);
    const nextCount = countEvents();
    if (nextCount <= lastCount) break;
    lastCount = nextCount;
  }
  log.push('clicks:' + clicks);
  log.push('events:' + lastCount);
  log.push('height:' + document.body.scrollHeight);
  return log.join(' | ');
}
"""
    return [
        {"type": "wait", "milliseconds": 2000},
        {"type": "executeJavascript", "script": script},
        {"type": "wait", "milliseconds": 2500},
    ]


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


_LIEDERHALLE_KLASSIK_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const log = [];

  const cookieRe = /^(alle\s+akzeptieren|akzeptieren|alle\s+cookies\s+akzeptieren|einverstanden|zustimmen|alles\s+erlauben|alle\s+aktivieren|accept(?:\s+all)?|agree|got\s+it)$/i;
  for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"]')) {
    const t = (el.textContent || el.value || '').trim();
    if (!t || t.length > 60) continue;
    if (cookieRe.test(t)) {
      try { el.click(); log.push('cookie:' + t); break; } catch (e) {}
    }
  }
  await sleep(900);

  const klassikRe = /klassik\s*\/\s*kultur/i;
  let filterClicked = false;

  // Open any category dropdown/filter control that mentions Klassik/Kultur.
  for (const el of document.querySelectorAll('button, a, [role="button"], [role="combobox"], .select, .dropdown, label')) {
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (klassikRe.test(t) || /kategorie|genre|filter/i.test(t)) {
      try {
        el.scrollIntoView({block: 'center'});
        el.click();
        log.push('filter-open:' + t.slice(0, 40));
        await sleep(700);
        break;
      } catch (e) {}
    }
  }

  // Click the actual option if it is rendered in a dropdown/list.
  for (const el of document.querySelectorAll('a, button, label, li, [role="option"], [role="menuitem"], [role="checkbox"]')) {
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (klassikRe.test(t)) {
      try {
        el.scrollIntoView({block: 'center'});
        el.click();
        filterClicked = true;
        log.push('klassik:' + t.slice(0, 40));
        await sleep(1400);
        break;
      } catch (e) {}
    }
  }
  if (!filterClicked) log.push('klassik:not-found');

  const loadMoreRe = /mehr\s+(laden|anzeigen|veranstaltungen|events)|weitere\s+(veranstaltungen|events)|show\s+more|load\s+more/i;
  let lastHeight = 0;
  let clicks = 0;
  let rounds = 0;
  for (let i = 0; i < 45; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(800);
    for (const el of document.querySelectorAll('a, button, [role="button"]')) {
      if (el.offsetParent === null) continue;
      const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (loadMoreRe.test(t)) {
        try {
          el.scrollIntoView({block: 'center'});
          el.click();
          clicks++;
          await sleep(1200);
          break;
        } catch (e) {}
      }
    }
    const h = document.body.scrollHeight;
    rounds = i + 1;
    if (h === lastHeight) break;
    lastHeight = h;
  }
  log.push('rounds:' + rounds);
  log.push('clicks:' + clicks);
  log.push('height:' + document.body.scrollHeight);
  return log.join(' | ');
}
"""


def _liederhalle_actions() -> list[dict]:
    return [
        {"type": "wait", "milliseconds": 2000},
        {"type": "executeJavascript", "script": _LIEDERHALLE_KLASSIK_JS},
        {"type": "wait", "milliseconds": 2500},
    ]


def _tonhalle_actions() -> list[dict]:
    # Tonhalle's cards lazy-load reliably with Firecrawl's native scroll
    # actions. executeJavascript scrolling currently stalls after ~12 cards.
    return [{"type": "wait", "milliseconds": 2000}] + _scroll_actions(
        n=24, amount=2200, wait_ms=900
    )


def _koelner_actions() -> list[dict]:
    # Kölner Philharmonie fades cards into the DOM while the viewport moves.
    # Native Firecrawl scroll actions preserve that intersection-observer flow
    # better than the generic executeJavascript loop.
    return [{"type": "wait", "milliseconds": 2500}] + _scroll_actions(
        n=24, amount=2200, wait_ms=1000
    )


VENUE_OVERRIDES: dict[str, dict] = {
    "berliner_philharmonie": {
        "actions": _bp_actions,
        "wait_for_listing_count": 20,
    },
    "konzerthaus_berlin": {
        # Infinite-scroll calendar: each scroll triggers a lazy XHR load.
        # Season runs through ~May 2027 (~120 events).
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=30, settle_ms=2500),
        "listing_target": 120,
        "force_url_expand": True,
    },
    "glocke_bremen": {
        # Paginated listing (/page/2/, /page/3/ …): the JS loop clicks the
        # "Weiter" button to load the next batch inline until no more appear.
        # map() + HTML-anchor fallback pick up remaining event links.
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=20, settle_ms=2000),
        "force_url_expand": True,
    },
    "konzerthaus_dortmund": {
        # Full season on a single infinite-scroll page (last event July 2027).
        # No load-more button — pure scroll to end. High listing_target so the
        # LLM extracts the whole season in one pass.
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=40, settle_ms=2000),
        "listing_target": 150,
        "force_url_expand": True,
    },
    "elbphilharmonie_hamburg": {
        # Infinite-scroll programme list; detail pages are /de/programm/<slug>/<id>.
        # Detail text may be hidden behind "Weiterlesen"; open it before extraction.
        "listing_url": "https://www.elbphilharmonie.de/de/programm/LHHH/TICKETS/",
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=35, settle_ms=2500),
        "detail_actions": lambda: _expand_read_more_actions(),
        "listing_target": 120,
        "force_url_expand": True,
    },
    "festspielhaus_baden_baden": {
        # /programm/ uses pure infinite scroll (no load-more button). User reports
        # scrolling manually reaches events through April 2027 (~200 events).
        # Default _scroll_actions(n=12) stops at ~34 events. Use generic JS loop
        # which scrolls until page height stops growing.
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=40, settle_ms=2000),
        # All events live on /programm/ (map() returns 0 /veranstaltungen/ URLs),
        # so the listing LLM has to extract everything in one pass. Default cap of
        # 60 lost half the events — raise so we capture the full season.
        "listing_target": 150,
        "force_url_expand": True,
        "drop_unmatched_undated_discovery_stubs": True,
    },
    "gewandhaus_leipzig": {
        "actions": _gewandhaus_actions,
        "wait_for_listing_count": 20,
    },
    "alte_oper_frankfurt": {
        # Calendar card grid with lazy-loaded rows. Use native scroll actions:
        # the generic JS bottom-jump loop causes Firecrawl to return fewer cards.
        # Detail text may be hidden behind "Weiterlesen"; open it before extraction.
        "actions": lambda: _scroll_actions(n=24, amount=2200, wait_ms=1000),
        "detail_actions": lambda: _expand_read_more_actions(),
        "listing_target": 120,
        "force_url_expand": True,
    },
    "isarphilharmonie_muenchen": {
        # Use the Münchner Philharmoniker calendar for deeper season coverage;
        # Gasteig's room-filtered listing currently stops around July.
        "listing_url": "https://www.mphil.de/kalender",
        "actions": _isarphi_actions,
        "listing_target": 150,
        "force_url_expand": True,
        "continue_url_discovery_on_hallucination": True,
        "drop_undated_discovery_stubs": True,
    },
    "koelner_philharmonie": {
        # Events fade in while scrolling; HTML anchors are the best source.
        "actions": _koelner_actions,
        "listing_target": 150,
        "force_url_expand": True,
    },
    "philharmonie_essen": {
        # Calendar starts around the current month; scrolling reaches later dates.
        # Detail URLs live under /programm/kalender/philharmonie-essen/<slug>/<id>/.
        "actions": lambda: _cookie_and_load_more_actions(max_rounds=35, settle_ms=2500),
        "listing_target": 150,
        "force_url_expand": True,
    },
    "liederhalle_stuttgart": {
        # Filter "Klassik / Kultur", then scroll/click "Mehr laden" to the end.
        "actions": _liederhalle_actions,
        "listing_target": 120,
        "force_url_expand": True,
    },
    "tonhalle_duesseldorf": {
        # Month-grouped cards; details at /veranstaltung/<series>/<id>-<slug>.
        # Traverse progressively; jumping to the bottom can leave lazy cards unloaded.
        "actions": _tonhalle_actions,
        "listing_target": 150,
        "force_url_expand": True,
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


_EXPAND_READ_MORE_JS = """
(() => {
  const labels = [
    'weiterlesen',
    'mehr lesen'
  ];
  const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
  let clicked = 0;
  for (const node of nodes) {
    const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || '')
      .replace(/\\s+/g, ' ')
      .trim()
      .toLowerCase();
    if (!text || !labels.some(label => text.includes(label))) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    node.click();
    clicked += 1;
  }
  return clicked;
})()
"""


def _expand_read_more_actions() -> list[dict]:
    return [
        {"type": "wait", "milliseconds": 1200},
        {"type": "executeJavascript", "script": _EXPAND_READ_MORE_JS},
        {"type": "wait", "milliseconds": 1200},
    ]


def _venue_detail_actions(slug: str) -> list[dict]:
    override = VENUE_OVERRIDES.get(slug)
    if override and callable(override.get("detail_actions")):
        actions = override["detail_actions"]()
    else:
        actions = []
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
    listing_url = VENUE_OVERRIDES.get(slug, {}).get("listing_url", venue["url"])
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    # html is requested alongside json/markdown — Firecrawl charges by the
    # most expensive format (json), so the extra html costs 0 credits.
    formats = [json_fmt, "markdown", "html"]
    actions = _venue_actions(slug)

    last_err: Exception | None = None

    def _attempt(extra: dict) -> dict:
        result = app.scrape(listing_url, formats=formats, **extra)
        md = _extract_markdown(result)
        if md:
            try:
                raw_store.save_markdown(slug, listing_url, md)
            except Exception as exc:
                print(f"    [warn] could not save raw markdown: {exc}", flush=True)
        extracted = _normalise(result) or {}
        html = _extract_html(result)
        if html:
            extracted = dict(extracted)
            extracted["_raw_html"] = html
        if md:
            extracted = dict(extracted)
            extracted["_raw_markdown"] = md
        return extracted

    # Try with scroll actions first, then without (some sites reject action requests)
    for with_actions in (True, False):
        extra: dict = {"headers": _DE_HEADERS}
        if with_actions:
            extra["actions"] = actions
        try:
            extracted = _attempt(extra)
            if (
                extracted.get("events")
                or extracted.get("_raw_html")
                or extracted.get("_raw_markdown")
            ):
                return extracted
        except TypeError as e:
            # unexpected kwarg (e.g. SDK doesn't accept headers) — retry without it
            if "headers" in str(e):
                extra.pop("headers", None)
                try:
                    extracted = _attempt(extra)
                    if (
                        extracted.get("events")
                        or extracted.get("_raw_html")
                        or extracted.get("_raw_markdown")
                    ):
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

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DE_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
_DE_SHORT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2})\b")
_DE_TEXT_DATE_RE = re.compile(
    r"(?i)\b"
    r"(?:(?:mo|di|mi|do|fr|sa|so|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\.?,?\s*)?"
    r"(\d{1,2})\.?\s+"
    r"(jan|januar|feb|februar|mär|märz|maerz|mrz|apr|april|mai|jun|juni|jul|juli|aug|august|"
    r"sep|sept|september|okt|oktober|nov|november|dez|dezember)"
    r"\s+(20\d{2})\b"
)
_TIME_RE = re.compile(r"(?:Uhrzeit\s*)?([0-2]?\d:[0-5]\d)\s*Uhr\b")
_DE_WEEKDAY_RE = r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
_DE_MONTHS = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mär": 3, "märz": 3, "maerz": 3, "mrz": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dezember": 12,
}


def _extract_event_urls_from_html(html: str, venue: dict) -> list[str]:
    """Mine the rendered HTML for venue-strict event-detail URLs.

    Some venues (festspielhaus.de) build event tiles client-side without
    populating sitemap.xml, so Firecrawl's map() endpoint returns nothing.
    But the rendered DOM does carry `<a href="/veranstaltungen/...">` anchors —
    we can lift detail URLs straight from there.
    """
    if not html:
        return []
    slug = _slug(venue["name"])
    base_url = venue["url"]
    seen: set[str] = set()
    out: list[str] = []
    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _HREF_RE.finditer(html))
    candidates.extend(match.group(1).strip() for match in _MARKDOWN_LINK_RE.finditer(html))
    candidates.extend(match.group(0).strip() for match in _URL_RE.finditer(html))

    for raw in candidates:
        raw = raw.split()[0].strip()
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, raw)
        # Strip query/fragment for dedupe — same event with ?date=... is the same page.
        clean = absolute.split("?", 1)[0].split("#", 1)[0]
        if clean in seen:
            continue
        if not is_strict_event_url(absolute, base_url, slug):
            continue
        seen.add(clean)
        out.append(absolute)
    return out


def _plain_markdown_text(value: str | None) -> str | None:
    if not value:
        return None
    value = value.replace("\\", "")
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or None


def _parse_de_date(value: str | None) -> str | None:
    match = _DE_DATE_RE.search(value or "")
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _parse_de_short_date(value: str | None) -> str | None:
    match = _DE_SHORT_DATE_RE.search(value or "")
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return date(2000 + int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _parse_de_month_name_date(day: str, month_name: str, horizon_date: str) -> str | None:
    month = _DE_MONTHS.get((month_name or "").strip().lower())
    if not month:
        return None
    today = _today_date()
    horizon = _parse_iso_date(horizon_date)
    years = [today.year, today.year + 1]
    for year in years:
        try:
            parsed = date(year, month, int(day))
        except ValueError:
            continue
        if parsed < today:
            continue
        if horizon and parsed > horizon:
            continue
        return parsed.isoformat()
    return None


def _extract_liederhalle_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Liederhalle event cards from Firecrawl markdown.

    TYPO3 renders stable markdown cards with headings and fields:
    "### Title", "Datum...", "Uhrzeit...", "Saal / Raum[...]".
    Parsing that locally is cheaper and more reliable than asking the LLM
    to infer dates for every URL-only discovery result.
    """
    if not rendered:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for raw_block in re.split(r"(?m)^###\s+", rendered)[1:]:
        lines = raw_block.splitlines()
        title = _plain_markdown_text(lines[0] if lines else None)
        if not title or _is_canceled(title):
            continue

        block = "\n".join(lines)
        event_urls = _extract_event_urls_from_html(block, venue)
        detail_url = _clean_url(event_urls[0]) if event_urls else ""
        if not detail_url or detail_url in seen:
            continue

        event_date = _parse_de_date(block)
        if not event_date or not _is_within_scrape_window(event_date, horizon_date):
            continue

        time_match = _TIME_RE.search(block)
        hall_match = re.search(r"Saal\s*/\s*Raum\s*\[([^\]]+)\]", block)
        if not hall_match:
            hall_match = re.search(r"Saal\s*/\s*Raum\s*([^\n\r]+)", block)

        ev = {
            "date": event_date,
            "time": time_match.group(1) if time_match else None,
            "title": title,
            "venue_hall": _plain_markdown_text(hall_match.group(1)) if hall_match else None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "liederhalle_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_tonhalle_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Tonhalle cards from Firecrawl markdown.

    Tonhalle's calendar cards are stable in markdown:
    thumbnail image, title, one or more short German dates, then repeated
    event links. Parsing these cards locally gives cheap, dated stubs and
    avoids relying on the LLM to return all 100+ visible cards.
    """
    if not rendered:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for block in re.split(r"(?m)^!\[\]\([^\n]+\)\s*", rendered)[1:]:
        urls = [_clean_url(url) for url in _extract_event_urls_from_html(block, venue)]
        urls = [url for i, url in enumerate(urls) if url and url not in urls[:i]]
        if not urls:
            continue

        title: str | None = None
        for line in block.splitlines()[:10]:
            candidate = _plain_markdown_text(line)
            if not candidate:
                continue
            if candidate.lower() == "mehr" or candidate.startswith("!"):
                continue
            if _DE_SHORT_DATE_RE.search(candidate) or _DE_DATE_RE.search(candidate):
                continue
            title = candidate
            break
        if not title or _is_canceled(title):
            continue

        event_date = _parse_de_short_date(block) or _parse_de_date(block)
        if not event_date or not _is_within_scrape_window(event_date, horizon_date):
            continue

        detail_url = urls[0]
        if detail_url in seen:
            continue

        ev = {
            "date": event_date,
            "time": None,
            "title": title,
            "venue_hall": None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "tonhalle_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_koelner_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Kölner Philharmonie cards from rendered markdown."""
    if not rendered:
        return []

    card_re = re.compile(
        r"(?ms)^-\s+(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+"
        r"(\d{2}\.\d{2}\.20\d{2})\s+"
        r"([0-2]?\d:[0-5]\d)\s+"
        r"(.*?)(?=^-\s+(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+\d{2}\.\d{2}\.20\d{2}|\Z)"
    )
    out: list[dict] = []
    seen: set[str] = set()
    slug = _slug(venue["name"])
    for match in card_re.finditer(rendered):
        event_date = _parse_de_date(match.group(1))
        if not event_date or not _is_within_scrape_window(event_date, horizon_date):
            continue
        block = match.group(3)
        urls = [_clean_url(url) for url in _extract_event_urls_from_html(block, venue)]
        detail_url = next(
            (
                url for url in urls
                if url and url not in seen and is_strict_event_url(url, venue["url"], slug)
            ),
            "",
        )
        if not detail_url:
            continue

        title = None
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("[!"):
                continue
            if detail_url in stripped:
                title = _plain_markdown_text(stripped)
                break
        title = title or _title_from_url(detail_url)
        if not title or _is_canceled(title):
            continue

        ev = {
            "date": event_date,
            "time": match.group(2),
            "title": title,
            "venue_hall": None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "koelner_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_essen_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Philharmonie Essen calendar cards from rendered markdown.

    The calendar renders one metadata block per event followed by a markdown
    h4 heading link:
    date, time range, hall, calendar link, series, then
    "#### [Title](detail_url)".
    """
    if not rendered:
        return []

    heading_re = re.compile(r"(?m)^####\s+\[([^\]]+)\]\((https?://[^)]+)\)")
    matches = list(heading_re.finditer(rendered))
    out: list[dict] = []
    seen: set[str] = set()
    previous_end = 0
    for match in matches:
        title = _plain_markdown_text(match.group(1))
        detail_url = _clean_url(match.group(2))
        if not title or _is_canceled(title) or not detail_url or detail_url in seen:
            previous_end = match.end()
            continue
        if not is_strict_event_url(detail_url, venue["url"], _slug(venue["name"])):
            previous_end = match.end()
            continue

        metadata = rendered[previous_end:match.start()]
        date_matches = list(_DE_DATE_RE.finditer(metadata))
        event_date = _parse_de_date(date_matches[-1].group(0)) if date_matches else None
        if not event_date or not _is_within_scrape_window(event_date, horizon_date):
            previous_end = match.end()
            continue

        time_matches = list(_TIME_RE.finditer(metadata))
        if not time_matches:
            time_matches = list(re.finditer(r"\b([0-2]?\d:[0-5]\d)\s*(?:-|$)", metadata))
        hall_matches = re.findall(
            r"(?m)^([^\n\r]{3,80}(?:Saal|Foyer|Pavillon|Philharmonie Essen))\s*$",
            metadata,
        )
        ev = {
            "date": event_date,
            "time": time_matches[-1].group(1) if time_matches else None,
            "title": title,
            "venue_hall": _plain_markdown_text(hall_matches[-1]) if hall_matches else None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "essen_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
        previous_end = match.end()
    return out


def _extract_alte_oper_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Alte Oper Frankfurt calendar cards from rendered markdown."""
    if not rendered:
        return []

    header_re = re.compile(
        rf"(?ms)(?:^|\s)(?:Mo|Di|Mi|Do|Fr|Sa|So)\s*"
        rf"(\d{{1,2}})\s*([A-Za-zÄÖÜäöüß]+)\s*(20\d{{2}})"
        rf"\s*([0-2]?\d:[0-5]\d)\s*([^\n\r\[]{{0,90}})"
    )
    matches = list(header_re.finditer(rendered))
    out: list[dict] = []
    seen: set[str] = set()
    slug = _slug(venue["name"])

    for i, match in enumerate(matches):
        day, month_name, year, event_time, hall = match.groups()
        month = _DE_MONTHS.get(month_name.strip().lower())
        if not month:
            continue
        try:
            event_date = date(int(year), month, int(day)).isoformat()
        except ValueError:
            continue
        if not _is_within_scrape_window(event_date, horizon_date):
            continue

        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(rendered)
        block = rendered[match.end():block_end]
        title = None
        detail_url = None
        for label, url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", block):
            clean = _clean_url(url)
            label_text = _plain_markdown_text(label)
            if (
                clean
                and label_text
                and not label.strip().startswith("!")
                and is_strict_event_url(clean, venue["url"], slug)
            ):
                title = label_text
                detail_url = clean
                break
        if not title or not detail_url or detail_url in seen or _is_canceled(title):
            continue

        price_match = re.search(r"\[(Ab\s+[^]]+€|[0-9][^]]*€)\]\(", block)
        ev = {
            "date": event_date,
            "time": event_time,
            "title": title,
            "venue_hall": _plain_markdown_text(hall),
            "program": [],
            "performers": [],
            "conductor": None,
            "price": _plain_markdown_text(price_match.group(1)) if price_match else None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "alte_oper_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_konzerthaus_berlin_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Konzerthaus Berlin cards from rendered markdown."""
    if not rendered:
        return []

    header_re = re.compile(
        rf"(?m)^\s*-\s*(?:(\d{{1,2}})\s*([A-Za-zÄÖÜäöü]+)\s+-\s*)?"
        rf"{_DE_WEEKDAY_RE}\s*([0-2]?\d)[.:]([0-5]\d)\s*Uhr\s*([^\n\r]*)"
    )
    matches = list(header_re.finditer(rendered))
    out: list[dict] = []
    seen: set[str] = set()
    current_date: str | None = None
    slug = _slug(venue["name"])
    for i, match in enumerate(matches):
        day, month_name, hour, minute, hall = match.groups()
        if day and month_name:
            current_date = _parse_de_month_name_date(day, month_name, horizon_date)
        if not current_date or not _is_within_scrape_window(current_date, horizon_date):
            continue

        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(rendered)
        block = rendered[match.end():block_end]
        title_match = re.search(r"(?m)^\s*#+\s+\[([^\]]+)\]\((https?://[^)]+)\)", block)
        if not title_match:
            continue
        title = _plain_markdown_text(title_match.group(1))
        detail_url = _clean_url(title_match.group(2))
        if (
            not title
            or _is_canceled(title)
            or not detail_url
            or detail_url in seen
            or not is_strict_event_url(detail_url, venue["url"], slug)
        ):
            continue

        ev = {
            "date": current_date,
            "time": f"{int(hour):02d}:{minute}",
            "title": title,
            "venue_hall": _plain_markdown_text(hall),
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "konzerthaus_berlin_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_elbphilharmonie_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Elbphilharmonie programme cards from rendered markdown."""
    if not rendered:
        return []

    card_re = re.compile(
        r"(?ms)^\s*-\s+\*\*(?:[A-Za-zÄÖÜäöüß]{2},\s*)?"
        r"(\d{1,2})\.(\d{1,2})\.(20\d{2})\*\*\s*"
        r"([0-2]?\d(?::[0-5]\d)?)\s*Uhr\s*"
        r"(.*?)(?=^\s*-\s+\*\*(?:[A-Za-zÄÖÜäöüß]{2},\s*)?\d{1,2}\.\d{1,2}\.20\d{2}\*\*|\Z)"
    )
    out: list[dict] = []
    seen: set[str] = set()
    slug = _slug(venue["name"])

    for match in card_re.finditer(rendered):
        day, month, year, event_time, block = match.groups()
        try:
            event_date = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        if not _is_within_scrape_window(event_date, horizon_date):
            continue

        hall_match = re.search(r"\*\*([^*\n]*(?:Elbphilharmonie|Laeiszhalle)[^*\n]*)\*\*", block)
        title = None
        detail_url = None
        for label, url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", block):
            clean = _clean_url(url)
            label_text = _plain_markdown_text(label)
            if not clean or not label_text or label.strip().startswith("!"):
                continue
            if "/ticket/" in clean:
                continue
            if is_strict_event_url(clean, venue["url"], slug):
                title = label_text
                detail_url = clean
                break
        if not title or not detail_url or detail_url in seen or _is_canceled(title):
            continue

        ev = {
            "date": event_date,
            "time": event_time if ":" in event_time else f"{int(event_time):02d}:00",
            "title": title,
            "venue_hall": _plain_markdown_text(hall_match.group(1)) if hall_match else None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": "Eintritt frei" if "Eintritt frei" in block else None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "elbphilharmonie_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_berliner_philharmonie_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Berliner Philharmoniker calendar cards from rendered markdown."""
    if not rendered:
        return []
    header_re = re.compile(
        r"(?ms)\*\*(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+"
        r"(\d{1,2})\.\s+([A-Za-zÄÖÜäöüß]+)\s+(20\d{2}),\s+"
        r"([0-2]?\d)[.:]([0-5]\d)\s+Uhr\*\*([^\n\r]*)"
    )
    matches = list(header_re.finditer(rendered))
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    slug = _slug(venue["name"])
    horizon = _parse_iso_date(horizon_date)
    today = _today_date()

    for i, match in enumerate(matches):
        day, month_name, year, hour, minute, hall = match.groups()
        month = _DE_MONTHS.get(month_name.strip().lower())
        if not month:
            continue
        try:
            event_date_obj = date(int(year), month, int(day))
        except ValueError:
            continue
        if event_date_obj < today or (horizon and event_date_obj > horizon):
            continue
        event_date = event_date_obj.isoformat()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(rendered)
        block = rendered[match.end():block_end]

        detail_match = re.search(
            r"\[Mehr lesen\]\((https?://www\.berliner-philharmoniker\.de/konzerte/kalender/\d+/?)\)",
            block,
        )
        detail_url = _clean_url(detail_match.group(1)) if detail_match else None
        title = None
        for bold_text in re.findall(r"\*\*([^*\n][^*]{1,120})\*\*", block):
            label = _plain_markdown_text(bold_text)
            if not label:
                continue
            low = label.lower()
            if low in {"werke von", "programm"} or "dirigent" in low or len(label) < 3:
                continue
            title = label
            break
        if not title or not detail_url or _is_canceled(title):
            continue

        key = (event_date, f"{int(hour):02d}:{minute}", detail_url)
        if key in seen:
            continue
        seen.add(key)
        program: list[str] = []

        ev = {
            "date": event_date,
            "time": f"{int(hour):02d}:{minute}",
            "title": title,
            "venue_hall": _plain_markdown_text(hall),
            "program": program,
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "berliner_philharmonie_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        out.append(ev)
    return out


def _extract_glocke_listing_events(
    rendered: str,
    venue: dict,
    horizon_date: str,
) -> list[dict]:
    """Parse Glocke Bremen cards from rendered markdown."""
    if not rendered:
        return []
    if "<" in rendered and "href=" in rendered:
        def _anchor_to_markdown(match: re.Match) -> str:
            href = match.group(1)
            label = _plain_markdown_text(re.sub(r"<[^>]+>", " ", match.group(2))) or ""
            return f"## [{label}]({href})"

        rendered = re.sub(
            r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
            _anchor_to_markdown,
            rendered,
        )
        rendered = re.sub(r"(?i)<br\s*/?>", "\n", rendered)
        rendered = re.sub(r"(?i)</(?:div|article|section|li|h\d|p)>", "\n", rendered)
        rendered = re.sub(r"<[^>]+>", " ", rendered)
        rendered = re.sub(r"&nbsp;", " ", rendered)
        rendered = re.sub(r"&amp;", "&", rendered)

    card_re = re.compile(
        rf"(?ms)^\s*{_DE_WEEKDAY_RE}\s*(\d{{1,2}})\s+\*{{0,2}}([A-Za-zÄÖÜäöü]+)\*{{0,2}}\s*"
        rf"(.*?)(?=^\s*{_DE_WEEKDAY_RE}\s*\d{{1,2}}\s+\*{{0,2}}[A-Za-zÄÖÜäöü]+|\Z)"
    )
    out: list[dict] = []
    seen: set[str] = set()
    slug = _slug(venue["name"])
    for match in card_re.finditer(rendered):
        event_date = _parse_de_month_name_date(match.group(1), match.group(2), horizon_date)
        if not event_date or not _is_within_scrape_window(event_date, horizon_date):
            continue
        block = match.group(3)
        urls = [_clean_url(url) for url in _extract_event_urls_from_html(block, venue)]
        detail_url = next(
            (
                url for url in urls
                if url and url not in seen and is_strict_event_url(url, venue["url"], slug)
            ),
            "",
        )
        if not detail_url:
            continue

        title_match = re.search(r"(?m)^#+\s+\[([^\]]+)\]\((https?://[^)]+)\)", block)
        title = _plain_markdown_text(title_match.group(1)) if title_match else None
        title = title or _title_from_url(detail_url)
        if not title or _is_canceled(title):
            continue

        time_match = _TIME_RE.search(block)
        hall_match = re.search(r"(?:####\s*)?[0-2]?\d:[0-5]\d\s*Uhr\s*(?:\\\||\|)?\s*([^\n\r<]+)", block)
        ev = {
            "date": event_date,
            "time": time_match.group(1) if time_match else None,
            "title": title,
            "venue_hall": _plain_markdown_text(hall_match.group(1)) if hall_match else None,
            "program": [],
            "performers": [],
            "conductor": None,
            "price": None,
            "detail_url": detail_url,
            "venue": venue["name"],
            "city": venue["city"],
            "discovery_source": "glocke_listing",
            "discovered_only": True,
        }
        _mark_enrichment_status(ev)
        seen.add(detail_url)
        out.append(ev)
    return out


def _extract_essen_schedule_candidate_urls(
    rendered: str,
    horizon_date: str,
) -> list[str]:
    """Find Essen calendar day URLs that can reveal more lazy-loaded cards."""
    today = _today_date()
    horizon = _parse_iso_date(horizon_date) or today
    by_date: dict[str, set[str]] = {}
    for match in re.finditer(
        r"https://www\.theater-essen\.de/programm/kalender/"
        r"(20\d{2}-\d{2})/?\?scheduleScrollTo=(20\d{2}-\d{2}-\d{2})",
        rendered or "",
    ):
        month, day = match.groups()
        parsed = _parse_iso_date(day)
        if not parsed or parsed < today or parsed > horizon:
            continue
        urls = by_date.setdefault(day, set())
        urls.add(
            "https://www.theater-essen.de/programm/kalender/"
            f"{month}/philharmonie-essen?scheduleScrollTo={day}"
        )
        if os.getenv("ESSEN_INCLUDE_GENERIC_DAY_URLS") == "1":
            urls.add(match.group(0))

    out: list[str] = []
    for day in sorted(by_date):
        # Try the venue-filtered URL before the generic day URL to reduce
        # unrelated Aalto/Schauspiel cards.
        out.extend(sorted(by_date[day], key=lambda url: ("philharmonie-essen" not in url, url)))
    return out


def _discover_glocke_paginated_urls(
    app,
    venue: dict,
    max_pages: int = 20,
    horizon_date: str | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Discover Glocke Bremen event URLs from its WordPress pagination.

    The listing is not true infinite scroll; "Weiter" maps to
    /tickets-programm/page/2/, /page/3/, ... . Fetching those HTML pages is
    deterministic and avoids spending Firecrawl credits just to find anchors.
    """
    slug = _slug(venue["name"])
    base = "https://www.glocke.de/tickets-programm/"
    horizon = horizon_date or _scrape_horizon_date()
    seen: set[str] = set()
    out: list[str] = []
    event_stubs: list[dict] = []
    empty_pages = 0
    failed_pages = 0
    for page in range(1, max_pages + 1):
        page_url = base if page == 1 else f"{base}page/{page}/"
        page_text = ""
        resp = None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = requests.get(page_url, headers=_DE_HEADERS, timeout=12)
                if resp.status_code >= 500 and attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
        if resp is None:
            print(f"    [glocke-pages] failed page {page}: {last_exc}", flush=True)
            try:
                result = app.scrape(page_url, formats=["markdown", "html"], headers=_DE_HEADERS)
            except TypeError:
                result = app.scrape(page_url, formats=["markdown", "html"])
            except Exception as exc:
                failed_pages += 1
                print(f"    [glocke-pages] firecrawl page {page} failed: {exc}", flush=True)
                if failed_pages >= 3:
                    break
                continue
            page_text = "\n".join([_extract_html(result), _extract_markdown(result)])
            if not page_text.strip():
                failed_pages += 1
                if failed_pages >= 3:
                    break
                continue
        if resp is not None and resp.status_code == 404:
            break
        if resp is not None and resp.status_code >= 400:
            failed_pages += 1
            print(f"    [glocke-pages] page {page}: HTTP {resp.status_code}", flush=True)
            if failed_pages >= 3:
                break
            continue
        if resp is not None:
            page_text = resp.text
        failed_pages = 0

        page_events = _extract_glocke_listing_events(page_text, venue, horizon)
        page_urls = [ev["detail_url"] for ev in page_events if ev.get("detail_url")]
        if not page_urls:
            page_urls = _extract_event_urls_from_html(page_text, venue)
        new_count = 0
        for url in page_urls:
            clean = url.split("?", 1)[0].split("#", 1)[0]
            if clean in seen:
                continue
            if not is_strict_event_url(clean, venue["url"], slug):
                continue
            seen.add(clean)
            out.append(clean)
            new_count += 1
        event_stubs.extend(
            ev for ev in page_events
            if _clean_url(ev.get("detail_url")) in seen
        )

        print(f"    [glocke-pages] page {page}: +{new_count} URLs", flush=True)
        if new_count == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
    dates = sorted({ev["date"] for ev in event_stubs if isinstance(ev.get("date"), str)})
    deduped_events: list[dict] = []
    seen_events: set[str] = set()
    for ev in event_stubs:
        clean = _clean_url(ev.get("detail_url"))
        if not clean or clean in seen_events:
            continue
        seen_events.add(clean)
        deduped_events.append(ev)
    return out, dates, deduped_events


def _discover_liederhalle_paginated_urls(
    app,
    venue: dict,
    max_pages: int = 20,
    horizon_date: str | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Discover Liederhalle event URLs from its rendered TYPO3 pagination.

    The listing exposes links like
    ?tx_bbevents_events[arguments][currentPage]=2. Plain requests sees only a
    shell, but Firecrawl-rendered page N includes all events through that page
    (page 12 reached 117 URLs / Nov 2026 in the probe run).
    """
    slug = _slug(venue["name"])
    base = "https://liederhalle.de/eventkalender"
    horizon = horizon_date or _scrape_horizon_date()
    candidate_pages = []
    for page in (max_pages, 12, 10, 8, 6, 4, 2, 1):
        if 1 <= page <= max_pages and page not in candidate_pages:
            candidate_pages.append(page)

    best_urls: list[str] = []
    best_dates: list[str] = []
    best_events: list[dict] = []
    for page in candidate_pages:
        if page == 1:
            page_url = base
        else:
            page_url = f"{base}?tx_bbevents_events%5Barguments%5D%5BcurrentPage%5D={page}"
        try:
            result = app.scrape(
                page_url,
                formats=["markdown", "html"],
                headers=_DE_HEADERS,
                actions=_venue_actions(slug),
            )
        except TypeError:
            result = app.scrape(page_url, formats=["markdown", "html"], actions=_venue_actions(slug))
        except Exception as exc:
            print(f"    [liederhalle-pages] failed page {page}: {exc}", flush=True)
            continue

        rendered = "\n".join([_extract_html(result), _extract_markdown(result)])
        page_urls_raw = _extract_event_urls_from_html(rendered, venue)
        page_events = _extract_liederhalle_listing_events(rendered, venue, horizon)
        seen: set[str] = set()
        page_urls: list[str] = []
        url_source = [ev["detail_url"] for ev in page_events if ev.get("detail_url")] or page_urls_raw
        for url in url_source:
            clean = url.split("?", 1)[0].split("#", 1)[0]
            if clean in seen:
                continue
            if not is_strict_event_url(clean, venue["url"], slug):
                continue
            seen.add(clean)
            page_urls.append(clean)
        page_dates = sorted(
            {ev["date"] for ev in page_events if isinstance(ev.get("date"), str)}
        ) or _extract_dates_from_text(rendered, horizon)

        print(
            f"    [liederhalle-pages] page {page}: {len(page_urls)} URLs "
            f"cards={len(page_events)} "
            f"dates={page_dates[0] if page_dates else None}..{page_dates[-1] if page_dates else None}",
            flush=True,
        )
        if len(page_urls) > len(best_urls):
            best_urls = page_urls
            best_dates = page_dates
            best_events = page_events
        if page_dates and page_dates[-1] >= horizon:
            break
    return best_urls, best_dates, best_events


def _iter_month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cur <= last:
        months.append(cur)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return months


def _discover_essen_monthly_urls(
    app,
    venue: dict,
    horizon_date: str | None = None,
    rendered_fallback: str | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Discover Philharmonie Essen detail URLs by iterating monthly calendars.

    Essen's calendar exposes month-specific, venue-filtered URLs:
    /programm/kalender/YYYY-MM/philharmonie-essen. Scrolling the generic
    page navigates through those months, but fetching them directly is more
    reliable and cheaper for URL discovery.
    """
    slug = _slug(venue["name"])
    horizon_date = horizon_date or _scrape_horizon_date()
    horizon = _parse_iso_date(horizon_date) or _today_date()
    seen: set[str] = set()
    out: list[str] = []
    event_stubs: list[dict] = []

    rendered = rendered_fallback or ""
    if not rendered:
        try:
            result = app.scrape(
                venue["url"],
                formats=["markdown", "html"],
                headers=_DE_HEADERS,
                actions=_venue_actions(slug),
            )
        except TypeError:
            result = app.scrape(venue["url"], formats=["markdown", "html"], actions=_venue_actions(slug))
        except Exception as exc:
            print(f"    [essen-listing] render failed: {exc}", flush=True)
            result = None
        if result is not None:
            rendered = "\n".join([_extract_html(result), _extract_markdown(result)])

    if rendered:
        listing_events = _extract_essen_listing_events(rendered, venue, horizon_date)
        for ev in listing_events:
            clean = _clean_url(ev.get("detail_url"))
            if not clean or clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
            event_stubs.append(ev)
        dates = sorted({ev["date"] for ev in event_stubs if isinstance(ev.get("date"), str)})
        print(
            f"    [essen-listing] {len(event_stubs)} cards "
            f"dates={dates[0] if dates else None}..{dates[-1] if dates else None}",
            flush=True,
        )
        if dates and dates[-1] >= horizon_date:
            return out, dates, event_stubs

        candidate_urls = _extract_essen_schedule_candidate_urls(rendered, horizon_date)
        latest_known = dates[-1] if dates else None
        if latest_known:
            filtered_candidates: list[str] = []
            for url in candidate_urls:
                match = re.search(r"scheduleScrollTo=(20\d{2}-\d{2}-\d{2})", url)
                if match and match.group(1) > latest_known:
                    filtered_candidates.append(url)
            candidate_urls = filtered_candidates
        max_candidates = _env_int("ESSEN_SCHEDULE_CANDIDATES", 18)
        candidate_urls = candidate_urls[:max_candidates]
        if candidate_urls:
            print(
                f"    [essen-days] probing {len(candidate_urls)} calendar day URL(s)",
                flush=True,
            )
        for idx, day_url in enumerate(candidate_urls, 1):
            try:
                result = app.scrape(
                    day_url,
                    formats=["markdown", "html"],
                    headers=_DE_HEADERS,
                    actions=_cookie_and_load_more_actions(max_rounds=12, settle_ms=1500),
                )
            except TypeError:
                result = app.scrape(
                    day_url,
                    formats=["markdown", "html"],
                    actions=_cookie_and_load_more_actions(max_rounds=12, settle_ms=1500),
                )
            except Exception as exc:
                print(f"    [essen-days] render failed {idx}/{len(candidate_urls)}: {exc}", flush=True)
                continue

            day_rendered = "\n".join([_extract_html(result), _extract_markdown(result)])
            day_events = _extract_essen_listing_events(day_rendered, venue, horizon_date)
            new_count = 0
            for ev in day_events:
                clean = _clean_url(ev.get("detail_url"))
                if not clean or clean in seen:
                    continue
                if not is_strict_event_url(clean, venue["url"], slug):
                    continue
                seen.add(clean)
                out.append(clean)
                event_stubs.append(ev)
                new_count += 1
            dates = sorted({ev["date"] for ev in event_stubs if isinstance(ev.get("date"), str)})
            day_match = re.search(r"scheduleScrollTo=(20\d{2}-\d{2}-\d{2})", day_url)
            print(
                f"    [essen-days] {day_match.group(1) if day_match else idx}: "
                f"+{new_count} cards latest={dates[-1] if dates else None}",
                flush=True,
            )
            if dates and dates[-1] >= horizon_date:
                return out, dates, event_stubs
            if dates and _is_near_horizon(dates[-1], horizon_date):
                print(
                    f"    [essen-days] stopping near horizon ({dates[-1]} vs {horizon_date})",
                    flush=True,
                )
                return out, dates, event_stubs

    for month in _iter_month_starts(_today_date(), horizon):
        page_url = (
            "https://www.theater-essen.de/programm/kalender/"
            f"{month:%Y-%m}/philharmonie-essen"
        )
        month_urls: list[str] = []
        try:
            resp = requests.get(page_url, headers=_DE_HEADERS, timeout=20)
            if resp.status_code < 400:
                month_urls = _extract_event_urls_from_html(resp.text, venue)
            else:
                print(f"    [essen-months] {month:%Y-%m}: HTTP {resp.status_code}", flush=True)
        except requests.RequestException as exc:
            print(f"    [essen-months] requests failed {month:%Y-%m}: {exc}", flush=True)

        if not month_urls:
            added_from_events = 0
            try:
                result = app.scrape(
                    page_url,
                    formats=["markdown", "html"],
                    headers=_DE_HEADERS,
                    actions=_cookie_and_load_more_actions(max_rounds=10, settle_ms=1500),
                )
            except TypeError:
                result = app.scrape(
                    page_url,
                    formats=["markdown", "html"],
                    actions=_cookie_and_load_more_actions(max_rounds=10, settle_ms=1500),
                )
            except Exception as exc:
                print(f"    [essen-months] render failed {month:%Y-%m}: {exc}", flush=True)
                result = None
            if result is not None:
                rendered = "\n".join([_extract_html(result), _extract_markdown(result)])
                month_events = _extract_essen_listing_events(rendered, venue, horizon_date)
                if month_events:
                    month_urls = [ev["detail_url"] for ev in month_events if ev.get("detail_url")]
                    for ev in month_events:
                        clean = _clean_url(ev.get("detail_url"))
                        if clean and clean not in seen and is_strict_event_url(clean, venue["url"], slug):
                            seen.add(clean)
                            out.append(clean)
                            event_stubs.append(ev)
                            added_from_events += 1
                    month_urls = []
                    if added_from_events:
                        print(
                            f"    [essen-months] {month:%Y-%m}: +{added_from_events} cards",
                            flush=True,
                        )
                else:
                    month_urls = _extract_event_urls_from_html(rendered, venue)

        new_count = 0
        for url in month_urls:
            clean = url.split("?", 1)[0].split("#", 1)[0]
            if clean in seen:
                continue
            if not is_strict_event_url(clean, venue["url"], slug):
                continue
            seen.add(clean)
            out.append(clean)
            new_count += 1
        print(f"    [essen-months] {month:%Y-%m}: +{new_count} URLs", flush=True)
    dates = sorted({ev["date"] for ev in event_stubs if isinstance(ev.get("date"), str)})
    return out, dates, event_stubs


def _discover_preferred_event_urls(
    app,
    venue: dict,
    html_fallback: str | None,
    horizon_date: str | None = None,
) -> tuple[list[str], dict, list[dict]]:
    """Venue-specific URL discovery that should outrank broad site map() results."""
    slug = _slug(venue["name"])
    urls: list[str] = []
    event_stubs: list[dict] = []
    stats: dict = {"latest_date": None, "date_count": 0}
    if html_fallback and slug == "tonhalle_duesseldorf":
        tonhalle_events = _extract_tonhalle_listing_events(
            html_fallback, venue, horizon_date or _scrape_horizon_date()
        )
        event_stubs.extend(tonhalle_events)
        urls.extend(ev["detail_url"] for ev in tonhalle_events if ev.get("detail_url"))
        tonhalle_dates = sorted(
            {ev["date"] for ev in tonhalle_events if isinstance(ev.get("date"), str)}
        )
        if tonhalle_dates:
            stats["latest_date"] = tonhalle_dates[-1]
            stats["date_count"] = len(tonhalle_dates)
        if not tonhalle_events:
            urls.extend(_extract_event_urls_from_html(html_fallback, venue))
    elif html_fallback and slug == "koelner_philharmonie":
        koelner_events = _extract_koelner_listing_events(
            html_fallback, venue, horizon_date or _scrape_horizon_date()
        )
        event_stubs.extend(koelner_events)
        urls.extend(ev["detail_url"] for ev in koelner_events if ev.get("detail_url"))
        koelner_dates = sorted(
            {ev["date"] for ev in koelner_events if isinstance(ev.get("date"), str)}
        )
        if koelner_dates:
            stats["latest_date"] = koelner_dates[-1]
            stats["date_count"] = len(koelner_dates)
        if not koelner_events:
            urls.extend(_extract_event_urls_from_html(html_fallback, venue))
    elif html_fallback and slug == "konzerthaus_berlin":
        khb_events = _extract_konzerthaus_berlin_listing_events(
            html_fallback, venue, horizon_date or _scrape_horizon_date()
        )
        event_stubs.extend(khb_events)
        urls.extend(ev["detail_url"] for ev in khb_events if ev.get("detail_url"))
        khb_dates = sorted(
            {ev["date"] for ev in khb_events if isinstance(ev.get("date"), str)}
        )
        if khb_dates:
            stats["latest_date"] = khb_dates[-1]
            stats["date_count"] = len(khb_dates)
        if not khb_events:
            urls.extend(_extract_event_urls_from_html(html_fallback, venue))
    elif html_fallback and slug == "glocke_bremen":
        glocke_events = _extract_glocke_listing_events(
            html_fallback, venue, horizon_date or _scrape_horizon_date()
        )
        event_stubs.extend(glocke_events)
        urls.extend(ev["detail_url"] for ev in glocke_events if ev.get("detail_url"))
        glocke_dates = sorted(
            {ev["date"] for ev in glocke_events if isinstance(ev.get("date"), str)}
        )
        if glocke_dates:
            stats["latest_date"] = glocke_dates[-1]
            stats["date_count"] = len(glocke_dates)
        if not glocke_events:
            urls.extend(_extract_event_urls_from_html(html_fallback, venue))
    elif html_fallback:
        urls.extend(_extract_event_urls_from_html(html_fallback, venue))
    if slug == "glocke_bremen":
        glocke_urls, glocke_dates, glocke_events = _discover_glocke_paginated_urls(
            app, venue, horizon_date=horizon_date
        )
        urls.extend(glocke_urls)
        event_stubs.extend(glocke_events)
        if glocke_dates:
            stats["latest_date"] = max(
                [d for d in (stats.get("latest_date"), glocke_dates[-1]) if d],
                default=None,
            )
            stats["date_count"] = max(stats.get("date_count", 0), len(glocke_dates))
    elif slug == "liederhalle_stuttgart":
        liederhalle_urls, liederhalle_dates, liederhalle_events = _discover_liederhalle_paginated_urls(
            app, venue, horizon_date=horizon_date
        )
        urls.extend(liederhalle_urls)
        event_stubs.extend(liederhalle_events)
        if liederhalle_dates:
            stats["latest_date"] = liederhalle_dates[-1]
            stats["date_count"] = len(liederhalle_dates)
    elif slug == "philharmonie_essen":
        essen_urls, essen_dates, essen_events = _discover_essen_monthly_urls(
            app, venue, horizon_date=horizon_date, rendered_fallback=html_fallback
        )
        urls.extend(essen_urls)
        event_stubs.extend(essen_events)
        if essen_dates:
            stats["latest_date"] = essen_dates[-1]
            stats["date_count"] = len(essen_dates)

    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        clean = url.split("?", 1)[0].split("#", 1)[0]
        if clean in seen:
            continue
        if not is_strict_event_url(clean, venue["url"], slug):
            continue
        seen.add(clean)
        deduped.append(clean)

    preferred_set = set(deduped)
    seen_events: set[str] = set()
    deduped_events: list[dict] = []
    for ev in event_stubs:
        clean = _clean_url(ev.get("detail_url"))
        if not clean or clean not in preferred_set or clean in seen_events:
            continue
        seen_events.add(clean)
        deduped_events.append(ev)
    return deduped, stats, deduped_events


def _extract_preferred_listing_events(
    rendered: str | None,
    venue: dict,
    horizon_date: str,
) -> tuple[list[dict], str | None]:
    """Parse venue-specific listing cards when the rendered HTML is structured."""
    if not rendered:
        return [], None
    slug = _slug(venue["name"])
    if slug == "tonhalle_duesseldorf":
        return _extract_tonhalle_listing_events(rendered, venue, horizon_date), "tonhalle_listing"
    if slug == "koelner_philharmonie":
        return _extract_koelner_listing_events(rendered, venue, horizon_date), "koelner_listing"
    if slug == "philharmonie_essen":
        return _extract_essen_listing_events(rendered, venue, horizon_date), "essen_listing"
    if slug == "alte_oper_frankfurt":
        return _extract_alte_oper_listing_events(rendered, venue, horizon_date), "alte_oper_listing"
    if slug == "konzerthaus_berlin":
        return _extract_konzerthaus_berlin_listing_events(rendered, venue, horizon_date), "konzerthaus_berlin_listing"
    if slug == "elbphilharmonie_hamburg":
        return _extract_elbphilharmonie_listing_events(rendered, venue, horizon_date), "elbphilharmonie_listing"
    if slug == "berliner_philharmonie":
        return _extract_berliner_philharmonie_listing_events(rendered, venue, horizon_date), "berliner_philharmonie_listing"
    if slug == "glocke_bremen":
        return _extract_glocke_listing_events(rendered, venue, horizon_date), "glocke_listing"
    return [], None


def _rendered_listing_text(listing: dict) -> str:
    """Return the rendered listing text Firecrawl gave us, HTML plus markdown."""
    if not isinstance(listing, dict):
        return ""
    return "\n".join(
        part for part in (listing.get("_raw_html"), listing.get("_raw_markdown")) if part
    )


def _unix_start_of_day(day: date) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp())


def _sample_probe_months(today: date, horizon: date) -> list[date]:
    months = [
        month for month in _iter_month_starts(today, horizon)
        if month != today.replace(day=1)
    ]
    if len(months) <= 3:
        return months
    sampled = [months[0], months[len(months) // 2], months[-1]]
    out: list[date] = []
    for month in sampled:
        if month not in out:
            out.append(month)
    return out


def _probe_candidate_urls(venue: dict, horizon_date: str) -> list[str]:
    """Generate cheap discovery candidates for hard calendar sites."""
    slug = _slug(venue["name"])
    today = _today_date()
    horizon = _parse_iso_date(horizon_date) or today
    urls: list[str] = [venue["url"]]

    if slug == "tonhalle_duesseldorf":
        base = "https://www.tonhalle.de/veranstaltungen/kalender"
        urls.extend([base, f"{base}?from=1778934644"])
        urls.extend(f"{base}?from={_unix_start_of_day(month)}" for month in _iter_month_starts(today, horizon))
    elif slug == "philharmonie_essen":
        urls.append("https://www.theater-essen.de/programm/kalender/")
        for month in _iter_month_starts(today, horizon):
            urls.append(f"https://www.theater-essen.de/programm/kalender/{month:%Y-%m}/philharmonie-essen")
            urls.append(
                "https://www.theater-essen.de/programm/kalender/"
                f"{month:%Y-%m}/philharmonie-essen?scheduleScrollTo={month:%Y-%m}-01"
            )
    elif slug == "liederhalle_stuttgart":
        base = "https://liederhalle.de/eventkalender"
        urls.extend(
            f"{base}?tx_bbevents_events%5Barguments%5D%5BcurrentPage%5D={page}"
            for page in range(2, 13)
        )
    elif slug == "elbphilharmonie_hamburg":
        urls.extend([
            "https://www.elbphilharmonie.de/de/programm/",
            "https://www.elbphilharmonie.de/de/programm//KON/",
            "https://www.elbphilharmonie.de/de/programm//KON/TICKETS/",
            "https://www.elbphilharmonie.de/de/programm/EHH/KON/",
            "https://www.elbphilharmonie.de/de/programm/EHH/KON/TICKETS/",
            "https://www.elbphilharmonie.de/de/programm/LHHH/TICKETS/",
        ])
    elif slug == "berliner_philharmonie":
        base = "https://www.berliner-philharmoniker.de/konzerte/kalender/"
        for month in _sample_probe_months(today, horizon):
            urls.append(f"{base}?from={month:%Y-%m}-01")
    elif slug == "alte_oper_frankfurt":
        # The common date/month/from query parameters do not change the rendered
        # card set here, so keep probe mode focused on stronger generic sources.
        pass
    elif slug == "gewandhaus_leipzig":
        urls.extend([
            "https://www.gewandhausorchester.de/spielplan/",
            "https://www.gewandhausorchester.de/veranstaltungen/",
        ])
    elif slug == "isarphilharmonie_muenchen":
        urls.extend([
            "https://www.gasteig.de/veranstaltungen/?room=isarphilharmonie",
            "https://www.mphil.de/kalender",
        ])

    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _extract_dates_from_text(text: str, horizon_date: str) -> list[str]:
    today = _today_date()
    horizon = _parse_iso_date(horizon_date) or today
    out: set[str] = set()
    for year, month, day in _ISO_DATE_RE.findall(text or ""):
        try:
            parsed = date(int(year), int(month), int(day))
        except ValueError:
            continue
        if today <= parsed <= horizon:
            out.add(parsed.isoformat())
    for day, month, year in _DE_DATE_RE.findall(text or ""):
        try:
            parsed = date(int(year), int(month), int(day))
        except ValueError:
            continue
        if today <= parsed <= horizon:
            out.add(parsed.isoformat())
    for day, month, year in _DE_SHORT_DATE_RE.findall(text or ""):
        try:
            parsed = date(2000 + int(year), int(month), int(day))
        except ValueError:
            continue
        if today <= parsed <= horizon:
            out.add(parsed.isoformat())
    for day, month_name, year in _DE_TEXT_DATE_RE.findall(text or ""):
        month = _DE_MONTHS.get(month_name.strip().lower())
        if not month:
            continue
        try:
            parsed = date(int(year), month, int(day))
        except ValueError:
            continue
        if today <= parsed <= horizon:
            out.add(parsed.isoformat())
    return sorted(out)


def _probe_text_preview(text: str, *, limit: int = 280) -> str | None:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return None
    return clean[:limit]


def _probe_one_url(app, venue: dict, probe_url: str, horizon_date: str) -> dict:
    slug = _slug(venue["name"])
    formats = ["markdown", "html"]
    result = None
    error = None
    try:
        result = app.scrape(
            probe_url,
            formats=formats,
            headers=_DE_HEADERS,
            actions=_venue_actions(slug),
        )
    except TypeError:
        try:
            result = app.scrape(probe_url, formats=formats, actions=_venue_actions(slug))
        except Exception as exc:
            error = str(exc)
    except Exception as exc:
        error = str(exc)

    html = _extract_html(result) if result is not None else ""
    md = _extract_markdown(result) if result is not None else ""
    text = "\n".join([html, md])
    urls = _extract_event_urls_from_html(text, venue)
    dates = _extract_dates_from_text(text, horizon_date)
    raw_lines = len(text.splitlines())
    return {
        "url": probe_url,
        "error": error,
        "raw_lines": raw_lines,
        "raw_preview": _probe_text_preview(text) if raw_lines <= 3 else None,
        "event_urls": len(urls),
        "unique_event_urls": len({u.split('?', 1)[0].split('#', 1)[0] for u in urls}),
        "first_date_seen": dates[0] if dates else None,
        "last_date_seen": dates[-1] if dates else None,
        "sample_urls": urls[:8],
    }


def _sample_evenly(values: list[str], limit: int) -> list[str]:
    if limit <= 0 or len(values) <= limit:
        return values[:]
    if limit == 1:
        return [values[0]]
    last = len(values) - 1
    indexes = sorted({round(i * last / (limit - 1)) for i in range(limit)})
    return [values[i] for i in indexes]


def _probe_one_detail_text(app, venue: dict, detail_url: str, horizon_date: str) -> dict:
    result = None
    error = None
    try:
        try:
            result = app.scrape(detail_url, formats=["markdown", "html"], headers=_DE_HEADERS)
        except TypeError:
            result = app.scrape(detail_url, formats=["markdown", "html"])
    except Exception as exc:
        error = str(exc)

    text = "\n".join([
        _extract_html(result) if result is not None else "",
        _extract_markdown(result) if result is not None else "",
    ])
    dates = _extract_dates_from_text(text, horizon_date)
    raw_lines = len(text.splitlines())
    return {
        "url": detail_url,
        "error": error,
        "raw_lines": raw_lines,
        "raw_preview": _probe_text_preview(text) if raw_lines <= 3 else None,
        "dates": dates[:6],
        "first_date_seen": dates[0] if dates else None,
        "last_date_seen": dates[-1] if dates else None,
    }


def _probe_sitemap_detail_pages(
    app,
    venue: dict,
    sitemap_urls: list[str],
    horizon_date: str,
    *,
    limit: int = 8,
) -> list[dict]:
    """Sample sitemap detail URLs to see whether sitemap expansion is viable.

    This is intentionally probe-only. It answers the scalable question:
    "Can detail pages expose dates without relying on the listing page?"
    """
    sampled_urls = _sample_evenly(sitemap_urls, limit)
    out: list[dict] = []
    for index, detail_url in enumerate(sampled_urls, start=1):
        print(f"    sitemap-detail {index}/{len(sampled_urls)}: {detail_url[:90]}", flush=True)
        probe = _probe_one_detail_text(app, venue, detail_url, horizon_date)
        print(
            f"      dates={probe['first_date_seen']}..{probe['last_date_seen']} "
            f"lines={probe['raw_lines']}",
            flush=True,
        )
        out.append(probe)
    return out


def _probe_sitemap_detail_llm_pages(
    app,
    venue: dict,
    detail_probes: list[dict],
    *,
    limit: int = 3,
) -> list[dict]:
    no_date_urls = [
        probe["url"] for probe in detail_probes
        if probe.get("url") and not probe.get("first_date_seen") and not probe.get("last_date_seen")
    ]
    sampled_urls = _sample_evenly(no_date_urls, limit)
    out: list[dict] = []
    for index, detail_url in enumerate(sampled_urls, start=1):
        print(f"    sitemap-detail-llm {index}/{len(sampled_urls)}: {detail_url[:90]}", flush=True)
        detail = _scrape_one_detail(app, detail_url, venue)
        probe = {
            "url": detail_url,
            "title": detail.get("title"),
            "date": detail.get("date"),
            "time": detail.get("time"),
            "program_count": len(detail.get("program") or []),
            "performer_count": len(detail.get("performers") or []),
            "has_price": bool(detail.get("price")),
        }
        print(
            f"      date={probe['date']} title={(probe.get('title') or '')[:60]}",
            flush=True,
        )
        out.append(probe)
    return out


def _probe_discovery(app, venue: dict, horizon_date: str) -> dict:
    slug = _slug(venue["name"])
    print(f"\n  [probe:{slug}] {venue['name']}", flush=True)
    probes: list[dict] = []
    sitemap_urls = _discover_sitemap_event_urls(venue)
    if sitemap_urls:
        print(f"    sitemap: {len(sitemap_urls)} strict event URLs", flush=True)
    sitemap_detail_probes = (
        _probe_sitemap_detail_pages(app, venue, sitemap_urls, horizon_date)
        if sitemap_urls
        else []
    )
    sitemap_detail_llm_probes = (
        _probe_sitemap_detail_llm_pages(app, venue, sitemap_detail_probes)
        if sitemap_detail_probes
        else []
    )
    for probe_url in _probe_candidate_urls(venue, horizon_date):
        print(f"    probe: {probe_url}", flush=True)
        probe = _probe_one_url(app, venue, probe_url, horizon_date)
        print(
            f"      urls={probe['unique_event_urls']} "
            f"lines={probe['raw_lines']} "
            f"dates={probe['first_date_seen']}..{probe['last_date_seen']}",
            flush=True,
        )
        probes.append(probe)

    best = max(probes, key=lambda p: (p.get("unique_event_urls") or 0, p.get("raw_lines") or 0), default={})
    sitemap_detail_date_hits = sum(
        1 for probe in sitemap_detail_probes
        if probe.get("first_date_seen") or probe.get("last_date_seen")
    )
    sitemap_detail_llm_date_hits = sum(
        1 for probe in sitemap_detail_llm_probes
        if probe.get("date")
    )
    return {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "scrape_horizon_date": horizon_date,
        "best_url": best.get("url"),
        "best_event_urls": best.get("unique_event_urls", 0),
        "best_last_date_seen": best.get("last_date_seen"),
        "sitemap_event_urls": len(sitemap_urls),
        "sample_sitemap_urls": sitemap_urls[:12],
        "sitemap_detail_probes": sitemap_detail_probes,
        "sitemap_detail_date_hits": sitemap_detail_date_hits,
        "sitemap_detail_llm_probes": sitemap_detail_llm_probes,
        "sitemap_detail_llm_date_hits": sitemap_detail_llm_date_hits,
        "probes": probes,
    }


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


def _extract_xml_locs(text: str) -> list[str]:
    if not text:
        return []
    return [
        re.sub(r"\s+", " ", loc).strip()
        for loc in re.findall(r"(?is)<loc>\s*(.*?)\s*</loc>", text)
    ]


def _fetch_text_url(url: str) -> str:
    try:
        response = requests.get(url, headers=_DE_HEADERS, timeout=15)
        if response.status_code >= 400:
            return ""
        return response.text or ""
    except Exception:
        return ""


def _discover_sitemap_event_urls(venue: dict, *, max_sitemaps: int = 80) -> list[str]:
    """Discover strict event URLs via robots.txt and sitemap XML without Firecrawl credits."""
    slug = _slug(venue["name"])
    parsed = urlparse(venue["url"])
    root = f"{parsed.scheme}://{parsed.netloc}"
    robots = _fetch_text_url(f"{root}/robots.txt")
    sitemap_urls = [
        line.split(":", 1)[1].strip()
        for line in robots.splitlines()
        if line.lower().startswith("sitemap:")
    ]
    sitemap_urls.extend([
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
        f"{root}/sitemap-index.xml",
    ])

    queue: list[str] = []
    seen_sitemaps: set[str] = set()
    seen_events: set[str] = set()
    event_urls: list[str] = []
    for sitemap_url in sitemap_urls:
        clean = sitemap_url.strip()
        if clean and clean not in seen_sitemaps:
            seen_sitemaps.add(clean)
            queue.append(clean)

    while queue and len(seen_sitemaps) <= max_sitemaps:
        sitemap_url = queue.pop(0)
        text = _fetch_text_url(sitemap_url)
        if not text:
            continue
        locs = _extract_xml_locs(text)
        for loc in locs:
            clean = _clean_url(loc)
            if not clean:
                continue
            if clean.endswith((".xml", ".xml.gz")):
                if clean not in seen_sitemaps and len(seen_sitemaps) < max_sitemaps:
                    seen_sitemaps.add(clean)
                    queue.append(clean)
                continue
            if not is_strict_event_url(clean, venue["url"], slug):
                continue
            if clean in seen_events:
                continue
            seen_events.add(clean)
            event_urls.append(clean)
    return event_urls


# ── Phase 2: Enrich event detail pages ───────────────────────────────────────

def _scrape_one_detail(app, detail_url: str, venue: dict) -> dict:
    """Scrape a single event detail page and return structured data."""
    prompt = _build_detail_prompt(venue)
    schema = EventDetail.model_json_schema()
    json_fmt = {"type": "json", "schema": schema, "prompt": prompt}
    actions = _venue_detail_actions(_slug(venue["name"]))
    kwargs = {"formats": [json_fmt]}
    if actions:
        kwargs["actions"] = actions
    try:
        try:
            result = app.scrape(detail_url, headers=_DE_HEADERS, **kwargs)
        except TypeError:
            result = app.scrape(detail_url, **kwargs)
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


def _clean_url(url) -> str:
    if not isinstance(url, str):
        return ""
    return url.strip().split("?", 1)[0].split("#", 1)[0]


def _has_enriched_data(ev: dict) -> bool:
    return bool(
        ev.get("program")
        or ev.get("performers")
        or ev.get("conductor")
        or ev.get("price")
        or ev.get("duration_min")
    )


def _mark_enrichment_status(ev: dict) -> None:
    enriched = _has_enriched_data(ev)
    ev["enriched"] = enriched
    ev["enrichment_status"] = "enriched" if enriched else "stub"


def _load_existing_event_cache(slug: str) -> dict[str, dict]:
    path = BASE / f"firecrawl_{slug}_events.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    events = data.get("events") if isinstance(data, dict) else []
    if not isinstance(events, list):
        return {}
    out: dict[str, dict] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        clean = _clean_url(ev.get("detail_url"))
        if clean:
            out[clean] = ev
    return out


def _merge_existing_event(ev: dict, cached: dict | None) -> None:
    if not cached:
        return
    copied = False
    for key in ("date", "time", "title", "venue_hall", "conductor", "price", "duration_min"):
        if not ev.get(key) and cached.get(key):
            ev[key] = cached[key]
            copied = True
    for key in ("program", "performers"):
        if not ev.get(key) and cached.get(key):
            ev[key] = cached[key]
            copied = True
    if copied and _has_enriched_data(cached):
        ev["reused_enrichment"] = True


def _reuse_existing_enrichment(events: list[dict], slug: str) -> list[dict]:
    cache = _load_existing_event_cache(slug)
    if not cache:
        return events
    reused = 0
    for ev in events:
        before = _has_enriched_data(ev)
        _merge_existing_event(ev, cache.get(_clean_url(ev.get("detail_url"))))
        if not before and _has_enriched_data(ev):
            reused += 1
    if reused:
        print(f"    reused enrichment from previous JSON: {reused} events", flush=True)
    return events


def _title_from_url(url: str) -> str:
    path_bits = [p for p in urlparse(url).path.split("/") if p]
    raw = path_bits[-1] if path_bits else "event"
    if raw.isdigit() and len(path_bits) > 1:
        raw = path_bits[-2]
    raw = re.sub(r"^\d{2}-\d{2}-20\d{2}-", "", raw)
    raw = re.sub(r"-20\d{2}-\d{2}-\d{2}(?:-\d{1,2})?(?:-\d{1,2})?(?:-\d{1,2})?$", "", raw)
    raw = re.sub(r"^\d+-", "", raw)
    raw = raw.replace("-", " ").strip()
    return raw.title() if raw else "Event"


def _event_title_key(title: str | None) -> str:
    """Loose title key for merging listing rows with URL-only stubs."""
    raw = (title or "").lower()
    raw = (
        raw.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"\b20\d{2}\b", " ", raw)
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _date_from_dortmund_url(url: str) -> str | None:
    match = re.search(r"/(\d{2})-(\d{2})-(20\d{2})-", urlparse(url).path)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _date_from_mphil_url(url: str) -> str | None:
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})(?:-|/?$)", urlparse(url).path)
    if not match:
        return None
    year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _event_stub_from_url(url: str, venue: dict, source: str) -> dict:
    clean = _clean_url(url)
    slug = _slug(venue["name"])
    event_date = None
    if slug == "konzerthaus_dortmund":
        event_date = _date_from_dortmund_url(clean)
    elif slug == "isarphilharmonie_muenchen":
        event_date = _date_from_mphil_url(clean)
    ev = {
        "date": event_date,
        "time": None,
        "title": _title_from_url(clean),
        "title_inferred": True,
        "venue_hall": None,
        "program": [],
        "performers": [],
        "conductor": None,
        "price": None,
        "detail_url": clean,
        "venue": venue["name"],
        "city": venue["city"],
        "discovery_source": source,
        "discovered_only": True,
    }
    _mark_enrichment_status(ev)
    return ev


def _date_from_event_url(url: str | None, slug: str) -> str | None:
    if not url:
        return None
    clean = _clean_url(url)
    if slug == "konzerthaus_dortmund":
        return _date_from_dortmund_url(clean)
    if slug == "isarphilharmonie_muenchen":
        return _date_from_mphil_url(clean)
    return None


def _fill_missing_dates_from_urls(events: list[dict], slug: str) -> int:
    filled = 0
    for ev in events:
        if ev.get("date"):
            continue
        inferred = _date_from_event_url(ev.get("detail_url"), slug)
        if inferred:
            ev["date"] = inferred
            filled += 1
    return filled


def _enrichment_priority(event: dict, original_index: int) -> tuple[int, str, str, int]:
    if has_classical_signal(event) and not has_non_classical_signal(event):
        bucket = 0
    elif is_classical_event(event) and not has_non_classical_signal(event):
        bucket = 1
    else:
        bucket = 2
    return (
        bucket,
        event.get("date") or "9999-99-99",
        event.get("time") or "",
        original_index,
    )


def _enrich_events(app, events: list[dict], venue: dict, limit: int | None = None) -> list[dict]:
    """
    For each event that has a detail_url but no program, scrape the detail
    page to fill in program, performers, conductor (and overwrite price/hall
    if listing didn't have them).
    """
    enriched_count = 0
    candidates_all = [
        (i, ev)
        for i, ev in enumerate(events)
        if not _has_enriched_data(ev) and _is_valid_url(ev.get("detail_url"))
    ]
    skipped_nonclassical = 0
    if limit is None:
        candidates = candidates_all
    else:
        candidates = []
        for i, ev in candidates_all:
            if has_non_classical_signal(ev):
                skipped_nonclassical += 1
                continue
            candidates.append((i, ev))
    if skipped_nonclassical:
        print(
            f"    skipping {skipped_nonclassical} clearly non-classical detail pages "
            f"to preserve enrichment budget",
            flush=True,
        )
    candidates.sort(key=lambda item: _enrichment_priority(item[1], item[0]))
    for i, ev in candidates:
        if limit is not None and enriched_count >= limit:
            break
        detail_url = ev.get("detail_url")
        print(f"    [{i+1}/{len(events)}] enriching: {ev.get('title','')[:55]}", flush=True)
        detail = _scrape_one_detail(app, detail_url, venue)
        if detail:
            enriched_count += 1
            ev["discovered_only"] = False
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

    for ev in events:
        _mark_enrichment_status(ev)
    return events


# ── Main per-venue scrape ─────────────────────────────────────────────────────

def _is_future(event_date: str | None) -> bool:
    if not event_date:
        return True  # keep events with missing dates (can't filter)
    parsed = _parse_iso_date(event_date)
    if parsed is None:
        return False
    return parsed >= _today_date()


def _is_within_scrape_window(event_date: str | None, horizon_date: str | None = None) -> bool:
    if not event_date:
        return True  # keep events with missing dates until detail/coverage can diagnose them
    parsed = _parse_iso_date(event_date)
    horizon = _parse_iso_date(horizon_date or _scrape_horizon_date())
    if parsed is None:
        return False
    if horizon is None:
        return True
    return _today_date() <= parsed <= horizon


def _latest_event_date(events: list[dict]) -> str | None:
    dates = sorted(
        e.get("date") for e in events
        if isinstance(e, dict) and _parse_iso_date(e.get("date"))
    )
    return dates[-1] if dates else None


def _is_near_horizon(event_date: str | None, horizon_date: str, grace_days: int = 1) -> bool:
    latest = _parse_iso_date(event_date)
    horizon = _parse_iso_date(horizon_date)
    if not latest or not horizon:
        return False
    return 0 <= (horizon - latest).days <= grace_days


def _covers_horizon_with_grace(event_date: str | None, horizon_date: str, grace_days: int = 1) -> bool:
    latest = _parse_iso_date(event_date)
    horizon = _parse_iso_date(horizon_date)
    if not latest or not horizon:
        return False
    return latest >= horizon or 0 <= (horizon - latest).days <= grace_days


def _add_coverage_fields(payload: dict, events: list[dict], horizon_date: str) -> None:
    latest = _latest_event_date(events)
    discovered_latest = payload.get("latest_discovered_event_date")
    if not isinstance(discovered_latest, str):
        discovered_latest = latest
    elif latest:
        discovered_latest = max(discovered_latest, latest)
    coverage_latest = max(
        [d for d in (latest, discovered_latest) if isinstance(d, str)],
        default=None,
    )
    payload["scrape_horizon_date"] = horizon_date
    payload["latest_event_date"] = latest
    payload["latest_discovered_event_date"] = discovered_latest
    payload["covers_horizon"] = _covers_horizon_with_grace(coverage_latest, horizon_date)
    payload["horizon_grace_days"] = 1

    total = len(events)
    dated = sum(
        1 for event in events
        if isinstance(event, dict) and _parse_iso_date(event.get("date"))
    )
    missing_dates = total - dated
    payload["events_with_date"] = dated
    payload["events_missing_date"] = missing_dates
    payload["date_coverage_rate"] = round(dated / total, 4) if total else None

    warnings: list[str] = []
    if coverage_latest and not payload["covers_horizon"]:
        warnings.append(f"SHORT_HORIZON(latest={coverage_latest}, horizon={horizon_date})")
    elif not coverage_latest and total:
        warnings.append(f"NO_DATED_EVENTS(horizon={horizon_date})")
    if total and missing_dates / total > 0.20:
        warnings.append(f"HIGH_UNDATED({missing_dates}/{total})")
    if warnings:
        payload["coverage_warning"] = "; ".join(warnings)
    else:
        payload.pop("coverage_warning", None)


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
    if not title or not _parse_iso_date(date):
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


def _discover_sitemap_detail_events(
    app,
    venue: dict,
    existing_events: list[dict],
    horizon_date: str,
    budget: int,
) -> tuple[list[dict], dict]:
    stats = {
        "budget": max(budget, 0),
        "sitemap_urls": 0,
        "scraped": 0,
        "new_events": 0,
        "missing_date_or_title": 0,
        "today_or_past": 0,
        "past_or_beyond_horizon": 0,
        "duplicate_urls": 0,
    }
    if budget <= 0:
        return [], stats

    sitemap_urls = _discover_sitemap_event_urls(venue)
    stats["sitemap_urls"] = len(sitemap_urls)
    if not sitemap_urls:
        return [], stats

    known = {
        _clean_url(event.get("detail_url"))
        for event in existing_events
        if _clean_url(event.get("detail_url"))
    }
    candidate_urls = [
        url for url in sitemap_urls
        if _clean_url(url) and _clean_url(url) not in known
    ]
    stats["duplicate_urls"] = len(sitemap_urls) - len(candidate_urls)
    sampled_urls = _sample_evenly(candidate_urls, budget)
    out: list[dict] = []
    seen_new: set[str] = set()
    print(
        f"    phase 1d: sitemap detail discovery "
        f"({len(sampled_urls)}/{len(candidate_urls)} candidate URLs) ...",
        flush=True,
    )
    for index, detail_url in enumerate(sampled_urls, start=1):
        clean = _clean_url(detail_url)
        print(f"    [sitemap {index}/{len(sampled_urls)}] {clean[:80]}", flush=True)
        detail = _scrape_one_detail(app, clean, venue)
        stats["scraped"] += 1
        ev = _detail_to_event(detail, clean, venue)
        if not ev:
            stats["missing_date_or_title"] += 1
            continue
        if _is_canceled(ev.get("title") or ""):
            continue
        parsed_date = _parse_iso_date(ev.get("date"))
        if not parsed_date:
            stats["missing_date_or_title"] += 1
            continue
        if parsed_date <= _today_date():
            stats["today_or_past"] += 1
            continue
        if not _is_within_scrape_window(ev.get("date"), horizon_date):
            stats["past_or_beyond_horizon"] += 1
            continue
        if clean in known or clean in seen_new:
            stats["duplicate_urls"] += 1
            continue
        ev["discovery_source"] = "sitemap_detail"
        ev["discovered_only"] = False
        _mark_enrichment_status(ev)
        seen_new.add(clean)
        out.append(ev)

    stats["new_events"] = len(out)
    latest = _latest_event_date(out)
    if latest:
        stats["latest_date"] = latest
    print(f"    sitemap detail discovery result: +{len(out)} events", flush=True)
    return out, stats


def _expand_via_map(
    app,
    venue: dict,
    listing_events: list[dict],
    *,
    html_fallback: str | None = None,
    horizon_date: str | None = None,
    use_map: bool = True,
) -> tuple[list[dict], dict]:
    """
    Discover event URLs NOT already in `listing_events` and return lightweight
    stub events. Detail enrichment happens later and is limited by max_events.

    When map() yields nothing and `html_fallback` is provided, we mine the
    rendered listing HTML for venue-strict detail-page anchors instead.

    Returns:
      (new_events, stats) where new_events is the list to APPEND to listing_events.
    """
    stats = {
        "preferred_urls": 0, "map_urls": 0, "html_urls": 0, "new_urls": 0,
        "latest_discovered_date": _latest_event_date(listing_events),
    }
    slug = _slug(venue["name"])
    venue_override = VENUE_OVERRIDES.get(slug, {})

    preferred_urls, preferred_stats, preferred_events = _discover_preferred_event_urls(
        app, venue, html_fallback, horizon_date=horizon_date
    )
    preferred_dated_urls = {
        _clean_url(ev.get("detail_url"))
        for ev in preferred_events
        if _clean_url(ev.get("detail_url")) and _parse_iso_date(ev.get("date"))
    }
    preferred_event_by_url = {
        _clean_url(ev.get("detail_url")): ev
        for ev in preferred_events
        if _clean_url(ev.get("detail_url"))
    }
    stats["preferred_urls"] = len(preferred_urls)
    if preferred_stats.get("latest_date"):
        stats["latest_discovered_date"] = max(
            [d for d in (stats.get("latest_discovered_date"), preferred_stats["latest_date"]) if d],
            default=None,
        )
        stats["preferred_date_count"] = preferred_stats.get("date_count", 0)
    if preferred_urls:
        print(f"    [map-expand] preferred discovery: {len(preferred_urls)} URLs", flush=True)
    preferred_is_authoritative = bool(
        preferred_event_by_url
        and preferred_stats.get("date_count", 0) >= 30
        and horizon_date
        and _is_near_horizon(preferred_stats.get("latest_date"), horizon_date, grace_days=1)
    )

    loose_urls: list[str] = []
    strict_urls: list[str] = []
    if use_map:
        loose_urls, strict_urls = _discover_all_event_urls(app, venue)
    map_urls = strict_urls or loose_urls
    stats["map_urls"] = len(map_urls)
    html_urls = _extract_event_urls_from_html(html_fallback or "", venue)
    stats["html_urls"] = len(html_urls)
    raw_html_urls = html_urls
    if preferred_is_authoritative:
        if preferred_dated_urls:
            preferred_urls = [url for url in preferred_urls if _clean_url(url) in preferred_dated_urls]
            preferred_event_by_url = {
                url: ev for url, ev in preferred_event_by_url.items()
                if url in preferred_dated_urls
            }
            stats["preferred_dated_urls"] = len(preferred_dated_urls)
        # If a structured card parser already found a substantial dated set up
        # to the configured horizon, don't append naked URL stubs. Raw anchor
        # mining often sees future-season/hidden links without dates, which
        # bloats output and wastes enrichment budget.
        html_urls = []
        strict_urls = []
        loose_urls = []
        stats["html_urls"] = len(raw_html_urls)
        stats["html_urls_used"] = 0
        stats["preferred_authoritative"] = True

    # Preferred/listing URLs first: they are the venue's visible programme order.
    # map() often sees archives, categories or overly broad URL families.
    discovered_urls: list[str] = []
    seen_discovered: set[str] = set()
    for source_urls in (preferred_urls, strict_urls, html_urls, loose_urls):
        for url in source_urls or []:
            clean = url.split("?", 1)[0].split("#", 1)[0]
            if clean in seen_discovered:
                continue
            seen_discovered.add(clean)
            discovered_urls.append(clean)

    if not discovered_urls:
        print(f"    [map-expand] no event URLs discovered (preferred + map + html)", flush=True)
        return [], stats

    known = {
        _clean_url(e.get("detail_url"))
        for e in listing_events
        if e.get("detail_url")
    }
    new_urls = [u for u in discovered_urls if u and u not in known]
    stats["new_urls"] = len(new_urls)
    print(
        f"    [map-expand] {len(discovered_urls)} event URLs discovered "
        f"({len(new_urls)} new beyond listing)",
        flush=True,
    )
    if not new_urls:
        return [], stats

    new_events = []
    for url in new_urls:
        clean = _clean_url(url)
        if clean in preferred_event_by_url:
            ev = dict(preferred_event_by_url[clean])
            ev["discovery_source"] = ev.get("discovery_source") or "url_discovery"
            _mark_enrichment_status(ev)
        else:
            ev = _event_stub_from_url(url, venue, "url_discovery")
        if (
            venue_override.get("drop_undated_discovery_stubs")
            and ev.get("discovered_only")
            and not ev.get("date")
        ):
            stats["undated_discovery_stubs_dropped"] = (
                stats.get("undated_discovery_stubs_dropped", 0) + 1
            )
            continue
        if not _is_within_scrape_window(ev.get("date"), horizon_date):
            continue
        new_events.append(ev)
    new_latest = _latest_event_date(new_events)
    if new_latest:
        stats["latest_discovered_date"] = max(
            [d for d in (stats.get("latest_discovered_date"), new_latest) if d],
            default=None,
        )
    print(f"    [map-expand] result: +{len(new_events)} discovered stubs", flush=True)
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
    horizon_date = _scrape_horizon_date()
    print(f"\n  [{slug}] {venue['name']} ({url}) — DISCOVERY mode", flush=True)

    payload: dict = {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "scrape_horizon_date": horizon_date,
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
        future = [e for e in scout_events if _is_within_scrape_window(e.get("date"), horizon_date)]
        if future:
            print(f"    JSON-LD: {len(future)} events within horizon found", flush=True)
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
                ld_future = [e for e in ld_events if _is_within_scrape_window(e.get("date"), horizon_date)]
                if len(ld_future) > len(events_raw):
                    print(
                        f"    JSON-LD (firecrawl-html): {len(ld_future)} upcoming events "
                        f"(prefer over {len(events_raw)} from listing LLM)",
                        flush=True,
                    )
                    events_raw = ld_future
                    total_visible = len(ld_future)
                    source = "jsonld_firecrawl_html"

    rendered_listing = _rendered_listing_text(listing)
    if rendered_listing:
        parsed_events, parsed_source = _extract_preferred_listing_events(
            rendered_listing, venue, horizon_date
        )
        if parsed_events and len(parsed_events) > len(events_raw or []):
            print(
                f"    listing parser: {len(parsed_events)} events from rendered cards "
                f"(prefer over {len(events_raw or [])} from {source})",
                flush=True,
            )
            events_raw = parsed_events
            total_visible = len(parsed_events)
            source = parsed_source or source

    if not events_raw:
        payload["error"] = "no events extracted from listing"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_discovered"] = 0
        payload["source"] = source
        _add_coverage_fields(payload, [], horizon_date)
        return payload

    # Filter past + canceled events
    events: list[dict] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        if not _is_within_scrape_window(e.get("date"), horizon_date):
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
    _add_coverage_fields(payload, events, horizon_date)
    return payload


def _scrape_one(
    app,
    venue: dict,
    max_events: int,
    enrich: bool = True,
    *,
    skip_map: bool = False,
    expand_via_map: bool = False,
    sitemap_detail_budget: int = 0,
) -> dict:
    slug = _slug(venue["name"])
    url = venue["url"]
    horizon_date = _scrape_horizon_date()
    print(f"\n  [{slug}] {venue['name']} ({url})", flush=True)

    payload: dict = {
        "venue": venue["name"],
        "city": venue["city"],
        "slug": slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "scrape_horizon_date": horizon_date,
        "engine": "firecrawl",
    }

    events_raw: list[dict] | None = None
    total_visible: int | None = None
    source: str = "firecrawl_listing"

    # ── Phase 0: JSON-LD scout (free, deterministic) ──
    print(f"    phase 0: JSON-LD scout ...", flush=True)
    scout_events = jsonld_scout.scout(url)
    if scout_events:
        future = [e for e in scout_events if _is_within_scrape_window(e.get("date"), horizon_date)]
        print(
            f"    JSON-LD: {len(scout_events)} events ({len(future)} within horizon)",
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
        # max_events is the detail-enrichment budget, not a discovery cap.
        # Listing recall is controlled by per-venue listing_target instead.
        venue_override = VENUE_OVERRIDES.get(_slug(venue["name"]), {})
        listing_target = venue_override.get("listing_target", 60)
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
            ld_future = [e for e in ld_events if _is_within_scrape_window(e.get("date"), horizon_date)]
            print(
                f"    JSON-LD (firecrawl-html): {len(ld_events)} events "
                f"({len(ld_future)} within horizon)",
                flush=True,
            )
            if ld_future:
                ld_future.sort(key=lambda e: (e.get("date") or "9999"))
                events_raw = ld_future
                total_visible = len(ld_future)
                source = "jsonld_firecrawl_html"

    rendered_listing = _rendered_listing_text(listing)
    if rendered_listing:
        parsed_events, parsed_source = _extract_preferred_listing_events(
            rendered_listing, venue, horizon_date
        )
        if parsed_events and len(parsed_events) > len(events_raw or []):
            print(
                f"    listing parser: {len(parsed_events)} events from rendered cards "
                f"(prefer over {len(events_raw or [])} from {source})",
                flush=True,
            )
            events_raw = parsed_events
            total_visible = len(parsed_events)
            source = parsed_source or source

    if not events_raw:
        payload["error"] = "no events extracted from listing"
        payload["events"] = []
        payload["total_events"] = 0
        payload["total_events_discovered"] = 0
        payload["source"] = source
        _add_coverage_fields(payload, [], horizon_date)
        return payload

    # Filter past + canceled events (prompt guard isn't always reliable)
    events: list[dict] = []
    for e in events_raw:
        if not isinstance(e, dict):
            continue
        if not _is_within_scrape_window(e.get("date"), horizon_date):
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
            "discovery_source": source,
            "discovered_only": False,
        }
        events.append(ev)
    events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))

    print(f"    listing: {len(events)} upcoming events (total_visible={total_visible})", flush=True)

    # Hallucination guard — refuse to save fabricated output
    venue_override = VENUE_OVERRIDES.get(_slug(venue["name"]), {})
    halluc = _check_hallucination(events, venue)
    if halluc:
        print(f"    HALLUCINATION_SUSPECTED: {halluc}", flush=True)
        if venue_override.get("continue_url_discovery_on_hallucination") and rendered_listing:
            print(
                "    discarding hallucinated listing events; continuing with URL discovery",
                flush=True,
            )
            events = []
            source = "url_discovery_after_hallucination"
        else:
            payload["error"] = f"hallucination suspected: {halluc}"
            payload["events"] = []
            payload["total_events"] = 0
            payload["total_events_visible"] = total_visible
            payload["total_events_discovered"] = 0
            _add_coverage_fields(payload, [], horizon_date)
            return payload

    # ── Phase 1c: URL expansion — discover beyond listing without enrichment ──
    map_stats: dict = {}
    new_from_map: list[dict] = []
    total_discovered_loose: int | None = total_visible
    total_discovered_strict: int | None = total_visible
    should_expand_urls = expand_via_map or bool(venue_override.get("force_url_expand"))
    if should_expand_urls and source not in ("jsonld", "jsonld_firecrawl_html"):
        # Only expand when listing was the source (JSON-LD is already complete).
        listing_html = None
        if isinstance(listing, dict):
            listing_html = "\n".join(
                part for part in (listing.get("_raw_html"), listing.get("_raw_markdown"))
                if isinstance(part, str)
            ) or None
        new_from_map, map_stats = _expand_via_map(
            app, venue, events,
            html_fallback=listing_html, horizon_date=horizon_date,
            use_map=not skip_map,
        )
        total_discovered_loose = (
            map_stats.get("preferred_urls")
            or map_stats.get("map_urls")
            or map_stats.get("html_urls")
            or total_visible
        )
        total_discovered_strict = (
            map_stats.get("preferred_urls")
            or map_stats.get("map_urls")
            or map_stats.get("html_urls")
            or total_visible
        )
    elif not skip_map:
        # Legacy stats-only path (no detail scraping).
        print(f"    phase 1b: site map for URL counts ...", flush=True)
        loose_urls, strict_urls = _discover_all_event_urls(app, venue)
        total_discovered_loose = len(loose_urls) if loose_urls else total_visible
        total_discovered_strict = len(strict_urls) if strict_urls else total_visible
    else:
        print(f"    phase 1b: skipped (--skip-map)", flush=True)

    # Merge listing + discovered URL stubs, dedupe by detail_url, sort by date.
    if new_from_map:
        seen_urls = {_clean_url(e.get("detail_url")) for e in events if e.get("detail_url")}
        listing_by_title: dict[str, dict] = {}
        for existing in events:
            key = _event_title_key(existing.get("title"))
            if key and key not in listing_by_title:
                listing_by_title[key] = existing
        for ev in new_from_map:
            clean = _clean_url(ev.get("detail_url"))
            if clean in seen_urls:
                continue
            title_match = listing_by_title.get(_event_title_key(ev.get("title")))
            if title_match:
                if not ev.get("date") and title_match.get("date"):
                    ev["date"] = title_match.get("date")
                    ev["time"] = ev.get("time") or title_match.get("time")
                    ev["venue_hall"] = ev.get("venue_hall") or title_match.get("venue_hall")
                if not title_match.get("detail_url") and clean:
                    title_match["detail_url"] = clean
                    title_match["program"] = title_match.get("program") or ev.get("program") or []
                    title_match["performers"] = (
                        title_match.get("performers") or ev.get("performers") or []
                    )
                    title_match["conductor"] = title_match.get("conductor") or ev.get("conductor")
                    title_match["price"] = title_match.get("price") or ev.get("price")
                    title_match["discovery_url_source"] = ev.get("discovery_source")
                    _mark_enrichment_status(title_match)
                    seen_urls.add(clean)
                    continue
            if (
                venue_override.get("drop_unmatched_undated_discovery_stubs")
                and ev.get("discovered_only")
                and not ev.get("date")
            ):
                map_stats["undated_unmatched_stubs_dropped"] = (
                    map_stats.get("undated_unmatched_stubs_dropped", 0) + 1
                )
                continue
            events.append(ev)
            seen_urls.add(clean)
        events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))
        print(f"    merged: {len(events)} total events after URL discovery", flush=True)

    sitemap_detail_stats: dict = {}
    if sitemap_detail_budget > 0:
        sitemap_events, sitemap_detail_stats = _discover_sitemap_detail_events(
            app, venue, events, horizon_date, sitemap_detail_budget
        )
        if sitemap_events:
            seen_urls = {_clean_url(e.get("detail_url")) for e in events if e.get("detail_url")}
            for ev in sitemap_events:
                clean = _clean_url(ev.get("detail_url"))
                if clean in seen_urls:
                    continue
                events.append(ev)
                seen_urls.add(clean)
            events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))
            print(f"    merged: {len(events)} total events after sitemap details", flush=True)

    events = _reuse_existing_enrichment(events, slug)
    filled_dates = _fill_missing_dates_from_urls(events, slug)
    if filled_dates:
        print(f"    filled {filled_dates} missing date(s) from event URLs", flush=True)

    # ── Phase 2: Enrich discovered events with detail pages ──
    if enrich and events and max_events > 0:
        print(
            f"    phase 2: enriching up to {max_events} discovered events "
            f"with detail pages ...",
            flush=True,
        )
        events = _enrich_events(app, events, venue, limit=max_events)
        filled_dates = _fill_missing_dates_from_urls(events, slug)
        if filled_dates:
            print(f"    filled {filled_dates} missing date(s) from event URLs", flush=True)
        events = [e for e in events if _is_within_scrape_window(e.get("date"), horizon_date)]
        events.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or ""))
    else:
        for ev in events:
            _mark_enrichment_status(ev)

    # Classify each event as classical (or jazz/opera/lieder) vs pop/musical/etc.
    for ev in events:
        ev["is_classical"] = is_classical_event(ev)

    payload["events"] = events
    payload["total_events"] = len(events)
    payload["total_events_visible"] = total_visible
    # Loose count (legacy field name — often inflated by map()).
    payload["total_events_discovered"] = total_discovered_loose
    payload["total_events_discovered_strict"] = total_discovered_strict
    payload["total_events_enriched"] = sum(1 for e in events if e.get("enriched"))
    payload["source"] = source
    if map_stats.get("latest_discovered_date"):
        payload["latest_discovered_event_date"] = map_stats["latest_discovered_date"]
    _add_coverage_fields(payload, events, horizon_date)
    if map_stats:
        payload["map_expand_stats"] = map_stats
    if sitemap_detail_stats:
        payload["sitemap_detail_expand_stats"] = sitemap_detail_stats
    return payload


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(
    slugs_filter: list[str] | None,
    max_events: int,
    enrich: bool,
    skip_map: bool = False,
    expand_via_map: bool = False,
    discover_mode: bool = False,
    probe_discovery: bool = False,
    enrich_count: int = 10,
    horizon_months: int = 6,
    sitemap_detail_budget: int = 0,
) -> None:
    os.environ["SCRAPE_HORIZON_MONTHS"] = str(horizon_months)
    horizon_date = _scrape_horizon_date(horizon_months)
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

    venues = get_venues_by_tier(0)
    if slugs_filter:
        venues = [v for v in venues if _slug(v["name"]) in slugs_filter]

    if not venues:
        print("No venues matched the filter.", file=sys.stderr)
        sys.exit(1)

    if probe_discovery:
        mode = "PROBE discovery (render candidate listing URLs, no event JSON overwrite)"
    elif discover_mode:
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
        if sitemap_detail_budget > 0:
            mode += f" + sitemap-detail-discovery({sitemap_detail_budget})"
    print(
        f"\nFire crawl scraping {len(venues)} Tier-0 venue(s), "
        f"max {max_events} events, horizon {horizon_date}. Mode: {mode}\n"
    )

    n_ok = n_fail = 0
    probe_results: list[dict] = []
    for venue in venues:
        if probe_discovery:
            probe_results.append(_probe_discovery(app, venue, horizon_date))
            n_ok += 1
            continue
        if discover_mode:
            result = _scrape_one_discover(app, venue, enrich_count=enrich_count)
        else:
            result = _scrape_one(
                app, venue, max_events, enrich=enrich,
                skip_map=skip_map, expand_via_map=expand_via_map,
                sitemap_detail_budget=sitemap_detail_budget,
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
                f"    OK: {result['total_events']} events saved "
                f"({prog}/{result['total_events']} with program), "
                f"discovered={disc} → {out_path.name}"
            )

    if probe_discovery:
        out_dir = BASE / "probes"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
        out_path = out_dir / f"firecrawl_probe_{ts}.json"
        payload = {
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "scrape_horizon_date": horizon_date,
            "results": probe_results,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\nProbe report written: {out_path}")

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
        "--probe-discovery",
        action="store_true",
        help=(
            "Render candidate listing URLs and report URL/date coverage only. "
            "Writes backend/probes/firecrawl_probe_*.json and does not overwrite "
            "backend/firecrawl_*_events.json."
        ),
    )
    parser.add_argument(
        "--enrich-count",
        type=int,
        default=10,
        help="Discovery mode: number of random events to fully enrich (default 10).",
    )
    parser.add_argument(
        "--horizon-months",
        type=int,
        default=_env_int("SCRAPE_HORIZON_MONTHS", 6),
        help=(
            "Scrape through the end of the month N months from today "
            "(default: 6, or SCRAPE_HORIZON_MONTHS)."
        ),
    )
    parser.add_argument(
        "--sitemap-detail-budget",
        type=int,
        default=_env_int("SITEMAP_DETAIL_BUDGET", 0),
        help=(
            "Optional discovery budget: scrape up to N strict sitemap detail "
            "pages as full event details. Default 0."
        ),
    )
    args = parser.parse_args()
    main(
        args.only, args.max_events,
        enrich=not args.skip_enrich,
        skip_map=args.skip_map,
        expand_via_map=args.expand_via_map,
        discover_mode=args.discover_mode,
        probe_discovery=args.probe_discovery,
        enrich_count=args.enrich_count,
        horizon_months=args.horizon_months,
        sitemap_detail_budget=args.sitemap_detail_budget,
    )
