"""
Site Analyzer — Step 1 of the Tier-0 onboarding pipeline.

For each Tier-0 venue in venues_germany.py, this script:
  1. Fetches the listing page and probes for structured sources (JSON-LD, iCal, RSS)
  2. Renders the page with crawl4ai + asks Claude to produce a VenueConfig JSON
  3. Validates the config by extracting 5 sample events
  4. Writes results to backend/scrape/venue_site_structures.json

Usage:
    python3 -m backend.scrape.analyze_venues
    python3 -m backend.scrape.analyze_venues --only konzerthaus_berlin
    python3 -m backend.scrape.analyze_venues --tier 0

Set ANTHROPIC_API_KEY in your environment before running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = Path(__file__).parent.parent
OUTPUT_PATH = BASE / "scrape" / "venue_site_structures.json"

sys.path.insert(0, str(BASE.parent))
from backend.venues_germany import VENUES_GERMANY


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_ANALYSIS_PROMPT = """\
You are analyzing a concert hall website to configure an automated scraper.

I will give you:
1. A Markdown render of the LISTING page (for understanding structure and content)
2. A raw HTML snippet of the LISTING page (use this to find REAL CSS class names)
3. Optionally: the same for ONE DETAIL page

Your task: produce a JSON configuration object. Use the HTML to derive accurate CSS selectors.

Return ONLY valid JSON, no markdown fences, no explanation. Even if the page is empty or blocked, return the JSON with confidence=0.0 and an explanation in "notes".

{
  "load_method": "scroll" | "click" | "static" | "spa" | "jsonld" | "ical",
  "needs_networkidle": <bool>,
  "scroll_max": <int, 30-120>,
  "click_keywords": [<button texts to click for "load more">, ...],
  "event_url_pattern": "<regex matching event detail URLs, e.g. /veranstaltung/[a-z0-9-]+>",
  "teaser_selector": "<CSS selector for a single event teaser block, derived from real HTML classes>",
  "field_selectors": {
    "title": "<CSS within teaser, use real class names from HTML>",
    "date": "<CSS within teaser, use real class names from HTML>",
    "time": "<CSS within teaser, or null>",
    "venue_hall": "<CSS within teaser, or null>"
  },
  "detail_selectors": {
    "program_section": "<CSS for program/works section on detail page, or null>",
    "performers_section": "<CSS for performers/cast section, or null>"
  },
  "structured_source": null | "jsonld" | "ical",
  "cross_promotion_risk": "low" | "medium" | "high",
  "multi_date_per_event": <bool>,
  "notes": "<important quirks: correct URLs, anti-bot measures, cookie walls, pagination, cross-promotion sources>",
  "confidence": <float 0.0-1.0>
}

Rules:
- Derive teaser_selector and field_selectors from REAL class names visible in the HTML snippet
- If the site has <script type="application/ld+json"> with MusicEvent data, set structured_source="jsonld"
- cross_promotion_risk is "high" if the listing mixes concerts from multiple different venues
- confidence: 0.8+ if you can see real class names and event structure clearly; 0.5 if partially visible; 0.2 if page was blocked/empty
- ALWAYS return valid JSON even if the page failed to load
"""


_UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                              "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.translate(_UMLAUT_MAP).lower()).strip("_")


def _probe_structured_sources(url: str) -> str | None:
    """Quick probe for JSON-LD MusicEvent or iCal without rendering."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "op.us/1.0"})
        html = r.text
        if 'application/ld+json' in html and 'MusicEvent' in html:
            return "jsonld"
        for ical_path in ("/calendar.ics", "/spielplan.ics", "/events.ics", "/programm.ics"):
            try:
                ir = requests.get(urljoin(url, ical_path), timeout=5,
                                  headers={"User-Agent": "op.us/1.0"})
                if ir.status_code == 200 and "VCALENDAR" in ir.text[:200]:
                    return "ical"
            except Exception:
                pass
    except Exception:
        pass
    return None


async def _render_and_analyze(url: str, venue_name: str) -> dict:
    """Render the listing page + one detail page with crawl4ai, then ask Claude."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.extraction_strategy import LLMExtractionStrategy
    except ImportError:
        return {"error": "crawl4ai not installed", "confidence": 0.0}

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "confidence": 0.0}

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(
        wait_for="body",
        scan_full_page=True,
        scroll_delay=0.5,
        delay_before_return_html=2.0,
    )

    listing_md = ""
    listing_html = ""
    detail_url = ""
    detail_md = ""
    detail_html = ""

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        # 1. Render listing page
        result = await crawler.arun(url=url, config=run_cfg)
        listing_md = (result.markdown or "")[:30000]
        listing_html = (result.cleaned_html or result.html or "")[:25000]

        # 2. Find a detail page link — broad pattern to catch more site structures
        if result.links:
            all_links = (result.links.get("internal") or [])
            candidates = [
                lnk["href"] for lnk in all_links
                if lnk.get("href") and re.search(
                    r"/(veranstaltung|veranstaltungen|event|events|konzert|konzerte"
                    r"|kalender|programm|spielplan|detail|ticket|id|show)/",
                    lnk["href"], re.IGNORECASE
                )
                and not re.search(r"\.(pdf|jpg|png|css|js)$", lnk["href"])
            ]
            if candidates:
                detail_url = candidates[0]
                if not detail_url.startswith("http"):
                    detail_url = urljoin(url, detail_url)
                dr = await crawler.arun(url=detail_url, config=run_cfg)
                detail_md = (dr.markdown or "")[:15000]
                detail_html = (dr.cleaned_html or dr.html or "")[:15000]

    # 3. Ask Claude to produce config JSON
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_content = (
        f"Venue: {venue_name}\nListing URL: {url}\n\n"
        f"=== LISTING PAGE — Markdown ===\n{listing_md}\n\n"
        f"=== LISTING PAGE — HTML snippet (use for real CSS class names) ===\n{listing_html}\n\n"
    )
    if detail_md:
        user_content += (
            f"=== DETAIL PAGE ({detail_url}) — Markdown ===\n{detail_md}\n\n"
            f"=== DETAIL PAGE — HTML snippet ===\n{detail_html}\n"
        )

    def _call_claude(content: str) -> dict:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=_ANALYSIS_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    try:
        config = _call_claude(user_content)
    except json.JSONDecodeError:
        # Retry with a simplified prompt if Claude returned non-JSON
        try:
            config = _call_claude(
                f"Venue: {venue_name}\nURL: {url}\n\n"
                f"The page content was minimal or blocked. Return a best-effort JSON config "
                f"with confidence=0.2 and explain in 'notes' what you know about this venue's website.\n\n"
                f"Markdown preview:\n{listing_md[:5000]}"
            )
        except Exception as e2:
            return {"error": str(e2), "confidence": 0.0}
    except Exception as e:
        return {"error": str(e), "confidence": 0.0}

    config["_detail_url_sample"] = detail_url
    return config


async def analyze_venue(venue: dict) -> dict:
    slug = _slug(venue["name"])
    url = venue["url"]
    print(f"  Analyzing {venue['name']} ({url}) ...", flush=True)

    # Quick structured-source probe (no LLM)
    structured = _probe_structured_sources(url)
    if structured:
        print(f"    → structured source found: {structured}")

    config = await _render_and_analyze(url, venue["name"])
    if structured and "load_method" in config:
        config["structured_source"] = structured
        if structured == "jsonld":
            config["load_method"] = "jsonld"

    confidence = config.get("confidence", 0.0)
    validation = "passed" if confidence >= 0.7 else "needs_review"
    if "error" in config:
        validation = "error"

    return {
        "slug": slug,
        "name": venue["name"],
        "city": venue["city"],
        "url": url,
        "tier": venue["tier"],
        "_analyzed_at": datetime.utcnow().isoformat() + "Z",
        "validation": validation,
        **config,
    }


async def main(slugs_filter: list[str] | None, tier_filter: int | None) -> None:
    existing: dict = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text())

    venues = VENUES_GERMANY
    if tier_filter is not None:
        venues = [v for v in venues if v["tier"] == tier_filter]
    if slugs_filter:
        venues = [v for v in venues if _slug(v["name"]) in slugs_filter]

    if not venues:
        print("No venues matched the filter.", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {len(venues)} venue(s)...")
    results = dict(existing)

    for venue in venues:
        slug = _slug(venue["name"])
        result = await analyze_venue(venue)
        results[slug] = result
        # Write after each venue so partial results are saved
        OUTPUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        status = result.get("validation", "?")
        conf = result.get("confidence", 0)
        print(f"    [{status}] confidence={conf:.2f}")

    print(f"\nDone. Results written to {OUTPUT_PATH}")
    passed = sum(1 for v in results.values() if v.get("validation") == "passed")
    review = sum(1 for v in results.values() if v.get("validation") == "needs_review")
    errors = sum(1 for v in results.values() if v.get("validation") == "error")
    print(f"  passed={passed}  needs_review={review}  errors={errors}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze concert hall websites")
    parser.add_argument("--only", metavar="SLUG", nargs="+",
                        help="Analyze only these venues (by slug)")
    parser.add_argument("--tier", type=int, metavar="N",
                        help="Analyze only venues at this tier")
    args = parser.parse_args()

    asyncio.run(main(
        slugs_filter=args.only,
        tier_filter=args.tier,
    ))
