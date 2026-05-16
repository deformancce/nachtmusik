"""
Venue scraper runner — Step 3 of the Tier-1 onboarding pipeline.

Loads a per-venue config from backend/scrape/configs/<slug>.py, runs crawl4ai
to extract events, validates them, and writes backend/<slug>_events.json.

Usage:
    python3 -m backend.scrape.run_scraper konzerthaus_berlin
    python3 -m backend.scrape.run_scraper konzerthaus_berlin --dry-run
    python3 -m backend.scrape.run_scraper --all-tier 1

Set ANTHROPIC_API_KEY in your environment.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import random
import re
import sys
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urljoin

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE.parent))

from backend.scrape._core.validators import (
    entry_has_mojibake, is_bad_title, title_matches_program, normalize_nfc
)
from backend.scrape._core.venue_resolver import resolve_hall
from backend.scrape._core.venue_config import VenueConfig

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_DETAIL_SYSTEM = """\
You are extracting structured data from a classical music concert detail page.

Return ONLY valid JSON matching this schema:
{
  "title": "<event title, or null if unknown>",
  "date": "<ISO date YYYY-MM-DD, or null>",
  "time": "<HH:MM 24h, or null>",
  "program": ["Composer: Work title (opus/catalog)", ...],
  "performers": ["Name (role)", ...],
  "conductor": "<name or null>",
  "duration_min": <int or null>,
  "intro": "<brief description, 1-2 sentences, or null>"
}

Rules:
- title should be the concert name, not the ticket button or page title
- program entries must be "Firstname Lastname: Full Work Title with opus"
- Include ALL works listed, not just the main one
- performers: include soloists and ensemble name, not the conductor
- If information is not present, use null for that field
- Do not invent information
"""


def _select_text(elem, selector) -> str:
    if not selector or elem is None:
        return ""
    try:
        found = elem.select_one(selector)
    except Exception:
        return ""
    return found.get_text(" ", strip=True) if found else ""


def _extract_teaser_url(teaser, base_url: str, pattern: str) -> str | None:
    """Find the best detail-page URL inside a teaser element."""
    matches = []
    for a in teaser.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        full = href if href.startswith("http") else urljoin(base_url, href)
        if pattern and not re.search(pattern, full, re.IGNORECASE):
            continue
        matches.append(full)
    if not matches:
        return None
    # Prefer the shortest matching URL (avoid /tickets/, /platzwahl/ trailing paths)
    return min(matches, key=len)


async def scrape_listing(config: VenueConfig) -> list[dict]:
    """Render the listing page with crawl4ai, then parse teasers with BeautifulSoup."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError:
        print("ERROR: crawl4ai not installed. Run: pip install crawl4ai", file=sys.stderr)
        sys.exit(1)
    from bs4 import BeautifulSoup

    browser_cfg = BrowserConfig(headless=True, verbose=False)

    # Wait strategy: avoid wait_for="networkidle" — many venues have
    # continuous analytics/polling traffic that never settles, causing 60s
    # timeouts. Use a fixed post-DCL delay instead. SPAs get a longer one.
    is_spa = config.load_method in ("spa", "scroll") or config.needs_networkidle
    delay = max(config.scroll_wait_s, 8.0 if is_spa else 2.0)

    scroll_cfg = dict(
        scan_full_page=(config.load_method in ("scroll", "spa")),
        scroll_delay=0.5,
        delay_before_return_html=delay,
        page_timeout=45000,
    )

    run_cfg = CrawlerRunConfig(**scroll_cfg)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=config.url, config=run_cfg)

    html = result.html or ""
    soup = BeautifulSoup(html, "lxml")

    pattern = config.event_url_pattern or ""
    fs = config.field_selectors or {}
    events: list[dict] = []
    seen: set[str] = set()

    # Primary path: teaser_selector + field_selectors
    if config.teaser_selector:
        try:
            teaser_elements = soup.select(config.teaser_selector)
        except Exception as e:
            print(f"  WARNING: teaser_selector {config.teaser_selector!r} invalid: {e}")
            teaser_elements = []

        for teaser in teaser_elements:
            detail_url = _extract_teaser_url(teaser, config.url, pattern)
            if not detail_url or detail_url in seen:
                continue
            seen.add(detail_url)
            events.append({
                "detail_url": detail_url,
                "title": _select_text(teaser, fs.get("title")),
                "date": _select_text(teaser, fs.get("date")),
                "time": _select_text(teaser, fs.get("time")),
                "venue_hall": _select_text(teaser, fs.get("venue_hall")),
            })

    # Fallback: URL-pattern over all internal links (the old behaviour)
    if not events:
        if config.teaser_selector:
            print(f"  WARNING: teaser_selector matched 0 events — falling back to URL pattern")
        all_links = (result.links or {}).get("internal") or []
        for link in all_links:
            href = link.get("href", "")
            if not href:
                continue
            if pattern and not re.search(pattern, href, re.IGNORECASE):
                continue
            full = href if href.startswith("http") else urljoin(config.url, href)
            if full in seen:
                continue
            seen.add(full)
            raw_title = link.get("text", "") or ""
            events.append({
                "detail_url": full,
                "title": " ".join(raw_title.split()),
                "date": "",
                "time": "",
                "venue_hall": "",
            })

    return events


async def enrich_event(
    event: dict,
    config: VenueConfig,
) -> dict:
    """Fetch the detail page and extract program/performers via Claude."""
    if not ANTHROPIC_API_KEY:
        return event

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError:
        return event

    detail_url = event["detail_url"]
    browser_cfg = BrowserConfig(headless=True, verbose=False)
    # Detail pages on SPA venues (Nuxt SSR, etc.) need more time to render
    # the program/performer sections, otherwise Claude only sees navigation.
    is_spa = config.load_method in ("spa", "scroll") or config.needs_networkidle
    detail_delay = 5.0 if is_spa else 1.5
    run_cfg = CrawlerRunConfig(
        wait_for="body",
        delay_before_return_html=detail_delay,
        page_timeout=30000,
    )

    try:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=detail_url, config=run_cfg)
        detail_md = (result.markdown or "")[:15000]
    except Exception as e:
        event["_error"] = str(e)
        return event

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    hint = config.claude_hint or ""
    system = _DETAIL_SYSTEM + (f"\n\nVenue context: {config.name}, {config.city}. {hint}" if hint else
                                f"\n\nVenue: {config.name}, {config.city}.")
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": f"URL: {detail_url}\n\n{detail_md}"}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        detail = json.loads(raw)
        # Claude title/date only fill in when the listing didn't provide them
        if not event.get("title") and detail.get("title"):
            event["title"] = detail["title"]
        if not event.get("date") and detail.get("date"):
            event["date"] = detail["date"]
        if not event.get("time") and detail.get("time"):
            event["time"] = detail["time"]
        event.update({
            "program": detail.get("program") or [],
            "performers": detail.get("performers") or [],
            "conductor": detail.get("conductor"),
            "duration_min": detail.get("duration_min"),
            "intro": detail.get("intro"),
        })
    except Exception as e:
        event["_detail_error"] = str(e)

    return event


def validate_events(events: list[dict], config: VenueConfig) -> list[dict]:
    """Apply quality guards and resolve venue/hall/city."""
    valid = []
    for ev in events:
        title = normalize_nfc(ev.get("title", ""))
        program = ev.get("program") or []

        if is_bad_title(title):
            continue
        if entry_has_mojibake(title) or any(entry_has_mojibake(p) for p in program if isinstance(p, str)):
            continue
        if program and not title_matches_program(title, program):
            continue

        # Venue/hall resolution
        raw_loc = ev.get("hall") or ev.get("venue_hall") or ""
        venue, hall, city = resolve_hall(
            raw_loc, config.hall_mappings, config.default_venue or config.name, config.default_city or config.city
        )
        ev["venue"] = venue
        ev["hall"] = hall
        ev["city"] = city

        valid.append(ev)
    return valid


def health_check(new_events: list[dict], output_path: Path) -> tuple[bool, list[str]]:
    """Compare with the previous run and flag anomalies."""
    if not output_path.exists():
        return True, []
    try:
        prev = json.loads(output_path.read_text()).get("events", [])
    except Exception:
        return True, []
    if not prev:
        return True, []

    n_now, n_prev = len(new_events), len(prev)
    alerts = []
    if n_now == 0:
        alerts.append("⛔ Zero events scraped")
    elif n_now < 0.5 * n_prev:
        alerts.append(f"⚠ Drop: {n_prev}→{n_now} events (>{50}% decrease)")

    empty = sum(1 for e in new_events if not e.get("program")) / max(n_now, 1)
    if empty > 0.5:
        alerts.append(f"⚠ Empty program rate {empty:.0%}")

    return len(alerts) == 0, alerts


async def run(slug: str, dry_run: bool = False, max_events: int | None = None) -> dict:
    # Load config
    config_module = importlib.import_module(f"backend.scrape.configs.{slug}")
    config: VenueConfig = config_module.CONFIG

    print(f"Scraping {config.name} ({config.url})", flush=True)

    # Step 1: Listing
    teasers = await scrape_listing(config)
    total_found = len(teasers)
    if max_events and len(teasers) > max_events:
        teasers = random.sample(teasers, max_events)
        print(f"  Found {total_found} event URLs on listing page (smoke: sampled {max_events} random)")
    else:
        print(f"  Found {total_found} event URLs on listing page")

    if not teasers:
        print("  WARNING: no events found — check event_url_pattern in config")
        return {"events": [], "total_events": 0, "venue": config.name, "scraped_at": datetime.utcnow().isoformat()}

    # Step 2: Enrich with detail pages (via Claude)
    events = []
    for i, teaser in enumerate(teasers):
        print(f"  [{i+1}/{len(teasers)}] {teaser.get('title', '')[:60]}", end="\r", flush=True)
        enriched = await enrich_event(teaser, config) if not dry_run else teaser
        events.append(enriched)

    print(f"\n  Enriched {len(events)} events")

    # Step 3: Validate
    valid = validate_events(events, config)
    print(f"  Valid after quality guards: {len(valid)}")

    # Step 4: Health check
    output_path = BASE / f"{slug}_events.json"
    ok, alerts = health_check(valid, output_path)
    for alert in alerts:
        print(f"  {alert}")

    # Step 5: Write output
    output = {
        "venue": config.name,
        "city": config.city,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "total_events": len(valid),
        "events": valid,
    }

    if not dry_run:
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"  Written to {output_path}")
    else:
        print("  [dry-run] output not written")
        # Show sample
        for ev in valid[:3]:
            print(f"    {ev.get('date','')} {ev.get('title','')[:60]}")

    return output


async def run_all_tier(tier: int, dry_run: bool, max_events: int | None = None) -> None:
    import importlib
    from backend.venues_germany import get_venues_by_tier
    venues = get_venues_by_tier(tier)
    slugs = [re.sub(r"[^a-z0-9]+", "_", v["name"].lower()).strip("_") for v in venues]

    configs_dir = BASE / "scrape" / "configs"
    available = [s for s in slugs if (configs_dir / f"{s}.py").exists()]
    missing = [s for s in slugs if s not in available]

    if missing:
        print(f"No config for: {', '.join(missing)} — run generate_configs.py first")
    if max_events:
        print(f"SMOKE MODE: capping at {max_events} events per venue")
    print(f"Running {len(available)} venue(s)...")

    for slug in available:
        try:
            await run(slug, dry_run=dry_run, max_events=max_events)
        except Exception as e:
            print(f"ERROR in {slug}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a venue scraper")
    parser.add_argument("slug", nargs="?", help="Venue slug (e.g. konzerthaus_berlin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing output files")
    parser.add_argument("--max-events", type=int, metavar="N",
                        help="Limit to N events (for testing)")
    parser.add_argument("--all-tier", type=int, metavar="N",
                        help="Run all venues at tier N that have a config")
    args = parser.parse_args()

    if args.all_tier is not None:
        asyncio.run(run_all_tier(args.all_tier, dry_run=args.dry_run, max_events=args.max_events))
    elif args.slug:
        asyncio.run(run(args.slug, dry_run=args.dry_run, max_events=args.max_events))
    else:
        parser.print_help()
        sys.exit(1)
