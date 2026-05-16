"""
Config generator — Step 2 of the Tier-1 onboarding pipeline.

Reads venue_site_structures.json and writes a Python config file per venue
into backend/scrape/configs/<slug>.py.

Existing config files are never overwritten unless --force is given.

Usage:
    python3 -m backend.scrape.generate_configs
    python3 -m backend.scrape.generate_configs --only konzerthaus_berlin
    python3 -m backend.scrape.generate_configs --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
STRUCTURES_PATH = BASE / "scrape" / "venue_site_structures.json"
CONFIGS_DIR = BASE / "scrape" / "configs"

_TEMPLATE = '''\
"""Auto-generated config for {name} — edit as needed, then commit.
Generated from venue_site_structures.json. DO NOT delete this file; instead edit it.
"""
from backend.scrape._core.venue_config import VenueConfig

CONFIG = VenueConfig(
    slug={slug!r},
    name={name!r},
    city={city!r},
    url={url!r},
    tier={tier!r},
    load_method={load_method!r},
    needs_networkidle={needs_networkidle!r},
    scroll_max={scroll_max!r},
    scroll_wait_s=2.0,
    click_keywords={click_keywords!r},
    event_url_pattern={event_url_pattern!r},
    teaser_selector={teaser_selector!r},
    field_selectors={field_selectors!r},
    detail_selectors={detail_selectors!r},
    default_venue={default_venue!r},
    default_city={city!r},
    hall_mappings={{}},
    cross_promotion_risk={cross_promotion_risk!r},
    multi_date_per_event={multi_date_per_event!r},
    notes={notes!r},
    confidence={confidence!r},
)
'''


def generate_config(slug: str, data: dict, force: bool) -> Path:
    out = CONFIGS_DIR / f"{slug}.py"
    if out.exists() and not force:
        print(f"  SKIP {slug}.py (already exists; use --force to overwrite)")
        return out

    ctx = {
        "slug": slug,
        "name": data.get("name", slug),
        "city": data.get("city", ""),
        "url": data.get("url", ""),
        "tier": data.get("tier", 1),
        "load_method": data.get("load_method", "scroll"),
        "needs_networkidle": data.get("needs_networkidle", False),
        "scroll_max": data.get("scroll_max", 60),
        "click_keywords": tuple(data.get("click_keywords") or []),
        "event_url_pattern": data.get("event_url_pattern", ""),
        "teaser_selector": data.get("teaser_selector", ""),
        "field_selectors": data.get("field_selectors") or {},
        "detail_selectors": data.get("detail_selectors") or {},
        "default_venue": data.get("name", ""),
        "cross_promotion_risk": data.get("cross_promotion_risk", "low"),
        "multi_date_per_event": data.get("multi_date_per_event", False),
        "notes": data.get("notes", ""),
        "confidence": data.get("confidence", 0.0),
    }
    out.write_text(_TEMPLATE.format(**ctx))
    status = "WRITE" if not out.exists() else "OVERWRITE"
    print(f"  {status} {out.name}  (confidence={ctx['confidence']:.2f})")
    return out


def main(only: list[str] | None, force: bool) -> None:
    if not STRUCTURES_PATH.exists():
        print(f"ERROR: {STRUCTURES_PATH} not found. Run analyze_venues.py first.",
              file=sys.stderr)
        sys.exit(1)

    structures = json.loads(STRUCTURES_PATH.read_text())
    CONFIGS_DIR.mkdir(exist_ok=True)

    for slug, data in structures.items():
        if only and slug not in only:
            continue
        if data.get("validation") not in ("passed", "needs_review"):
            print(f"  SKIP {slug} (validation={data.get('validation')})")
            continue
        generate_config(slug, data, force)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="SLUG")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing config files")
    args = parser.parse_args()
    main(only=args.only, force=args.force)
