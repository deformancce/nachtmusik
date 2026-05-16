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
  "program": ["Composer: Work title (opus/catalog)", ...],
  "performers": ["Name (role)", ...],
  "conductor": "<name or null>",
  "duration_min": <int or null>,
  "intro": "<brief description, 1-2 sentences, or null>"
}

Rules:
- program entries must be "Firstname Lastname: Full Work Title with opus"
- Include ALL works listed, not just the main one
- performers: include soloists and ensemble name, not the conductor
- If information is not present, use null for that field
- Do not invent information
"""


async def scrape_listing(config: VenueConfig) -> list[dict]:
    """Fetch the listing page and extract event teasers."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
    except ImportError:
        print("ERROR: crawl4ai not installed. Run: pip install crawl4ai", file=sys.stderr)
        sys.exit(1)

    browser_cfg = BrowserConfig(headless=True, verbose=False)

    scroll_cfg = dict(
        scan_full_page=(config.load_method in ("scroll", "spa")),
        scroll_delay=0.5,
        delay_before_return_html=config.scroll_wait_s,
    )
    if config.needs_networkidle:
        scroll_cfg["wait_for"] = "networkidle"

    run_cfg = CrawlerRunConfig(**scroll_cfg)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=config.url, config=run_cfg)

    # Extract event detail URLs from internal links
    all_links = []
    if result.links:
        all_links = result.links.get("internal") or []

    events = []
    pattern = config.event_url_pattern
    for link in all_links:
        href = link.get("href", "")
        if not href:
            continue
        if pattern and not re.search(pattern, href, re.IGNORECASE):
            continue
        full_url = href if href.startswith("http") else urljoin(config.url, href)
        events.append({
            "detail_url": full_url,
            "title": link.get("text", "").strip(),
        })

    # Deduplicate by URL
    seen = set()
    unique = []
    for ev in events:
        url = ev["detail_url"]
        if url not in seen:
            seen.add(url)
            unique.append(ev)

    return unique


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
    run_cfg = CrawlerRunConfig(wait_for="body", delay_before_return_html=1.0)

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
    if max_events:
        teasers = teasers[:max_events]
    print(f"  Found {len(teasers)} event URLs on listing page")

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


async def run_all_tier(tier: int, dry_run: bool) -> None:
    import importlib
    from backend.venues_germany import get_venues_by_tier
    venues = get_venues_by_tier(tier)
    slugs = [re.sub(r"[^a-z0-9]+", "_", v["name"].lower()).strip("_") for v in venues]

    configs_dir = BASE / "scrape" / "configs"
    available = [s for s in slugs if (configs_dir / f"{s}.py").exists()]
    missing = [s for s in slugs if s not in available]

    if missing:
        print(f"No config for: {', '.join(missing)} — run generate_configs.py first")
    print(f"Running {len(available)} venue(s)...")

    for slug in available:
        try:
            await run(slug, dry_run=dry_run)
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
        asyncio.run(run_all_tier(args.all_tier, dry_run=args.dry_run))
    elif args.slug:
        asyncio.run(run(args.slug, dry_run=args.dry_run, max_events=args.max_events))
    else:
        parser.print_help()
        sys.exit(1)
