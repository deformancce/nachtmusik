"""
Coverage report for Firecrawl (and optional legacy) venue scrape outputs.

Usage (from repo root):
    python3 -m backend.monitoring.coverage_report
    python3 -m backend.monitoring.coverage_report --write reports/coverage.md
    python3 -m backend.monitoring.coverage_report --glob 'backend/firecrawl_*_events.json'
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
DEFAULT_GLOB = "firecrawl_*_events.json"

sys.path.insert(0, str(REPO_ROOT))
from backend.scrape.classify import has_non_classical_signal, is_classical_event

# Legacy full scrapes for reference totals (optional).
LEGACY_REFERENCE: dict[str, str] = {
    "berliner_philharmonie": "berliner_philharmonie_events.json",
    "gewandhaus_leipzig": "gewandhaus_events.json",
}

NOISE_RATIO_WARN = 5.0  # discovered_loose / max(visible, extracted) above this → flag
MAP_ABSOLUTE_WARN = 250  # loose map count above this → flag regardless of ratio
CLASSICAL_PROGRAM_WARN = 0.60
NONCLASSICAL_ENRICH_WARN = 0.20


@dataclass
class VenueCoverage:
    slug: str
    venue: str
    source_url: str
    scraped_at: str
    engine: str
    error: str | None
    extracted: int
    visible: int | None
    discovered_loose: int | None
    discovered_strict: int | None
    reference_total: int | None
    with_program: int
    with_date: int
    with_detail_url: int
    classical: int
    classical_with_program: int
    enriched: int
    nonclassical_enriched: int
    discovered_only: int
    scrape_horizon_date: str | None
    latest_event_date: str | None
    latest_discovered_event_date: str | None
    covers_horizon: bool | None
    flags: list[str] = field(default_factory=list)

    @property
    def program_rate(self) -> float | None:
        if not self.extracted:
            return None
        return self.with_program / self.extracted

    @property
    def classical_program_rate(self) -> float | None:
        if not self.classical:
            return None
        return self.classical_with_program / self.classical

    @property
    def date_rate(self) -> float | None:
        if not self.extracted:
            return None
        return self.with_date / self.extracted

    @property
    def nonclassical_enrich_rate(self) -> float | None:
        if not self.enriched:
            return None
        return self.nonclassical_enriched / self.enriched

    @property
    def noise_ratio_loose(self) -> float | None:
        base = self.visible or self.extracted or 0
        if not base or self.discovered_loose is None:
            return None
        return self.discovered_loose / base

    @property
    def noise_ratio_strict(self) -> float | None:
        base = self.reference_total or self.visible or self.extracted or 0
        if not base or self.discovered_strict is None:
            return None
        return self.discovered_strict / base

    @property
    def discovery_status(self) -> str:
        if self.error or self.extracted == 0:
            return "FAIL"
        if self.covers_horizon is True:
            return "OK"
        if self.scrape_horizon_date is None:
            return "UNK"
        return "WARN"

    @property
    def dates_status(self) -> str:
        if self.extracted == 0:
            return "FAIL"
        return "OK" if self.with_date == self.extracted else "WARN"

    @property
    def program_status(self) -> str:
        rate = self.classical_program_rate
        if rate is None:
            return "UNK"
        return "OK" if rate >= CLASSICAL_PROGRAM_WARN else "WARN"

    @property
    def map_status(self) -> str:
        if any(f.startswith(("MAP_OVERCOUNT", "HIGH_MAP_NOISE", "STRICT_vs_LEGACY")) for f in self.flags):
            return "WARN"
        return "OK"

    @property
    def cost_status(self) -> str:
        rate = self.nonclassical_enrich_rate
        if rate is None:
            return "UNK"
        return "OK" if rate <= NONCLASSICAL_ENRICH_WARN else "WARN"

    @property
    def needs_adapter(self) -> bool:
        return any((
            self.discovery_status in ("FAIL", "WARN"),
            self.dates_status == "WARN",
            self.map_status == "WARN",
        ))

    @property
    def next_action(self) -> str:
        if self.error:
            return "fix run error"
        if self.extracted == 0:
            return "repair listing extraction"
        if self.with_date < self.extracted:
            return "add card/date parser"
        if self.covers_horizon is False:
            return "extend listing/pagination discovery"
        if self.map_status == "WARN":
            return "tighten URL pattern or adapter"
        if self.program_status == "WARN":
            return "improve detail enrichment"
        if self.cost_status == "WARN":
            return "refine budget priority"
        if self.scrape_horizon_date is None:
            return "rerun with current horizon"
        return "monitor"


def _slug_from_path(path: Path) -> str:
    name = path.stem
    if name.startswith("firecrawl_") and name.endswith("_events"):
        return name[len("firecrawl_") : -len("_events")]
    return name


def _load_reference_total(slug: str) -> int | None:
    rel = LEGACY_REFERENCE.get(slug)
    if not rel:
        return None
    path = BACKEND / rel
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    events = data.get("events")
    if isinstance(events, list):
        return len(events)
    return data.get("total_events")


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in {"null", "none", "n/a"}:
        return None
    return value


def _analyze_file(path: Path) -> VenueCoverage:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    slug = data.get("slug") or _slug_from_path(path)
    events = data.get("events") or []
    if not isinstance(events, list):
        events = []

    extracted = len(events)
    with_program = sum(1 for e in events if isinstance(e, dict) and e.get("program"))
    with_date = sum(1 for e in events if isinstance(e, dict) and _optional_str(e.get("date")))
    with_detail = sum(1 for e in events if isinstance(e, dict) and e.get("detail_url"))
    classical = 0
    classical_with_program = 0
    enriched = 0
    nonclassical_enriched = 0
    discovered_only = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        is_classical = event.get("is_classical")
        if not isinstance(is_classical, bool):
            is_classical = is_classical_event(event)
        has_enrichment = bool(
            event.get("program")
            or event.get("performers")
            or event.get("conductor")
            or event.get("price")
            or event.get("duration_min")
        )
        if is_classical:
            classical += 1
            if event.get("program"):
                classical_with_program += 1
        if has_enrichment:
            enriched += 1
            if not is_classical or has_non_classical_signal(event):
                nonclassical_enriched += 1
        if event.get("discovered_only"):
            discovered_only += 1
    latest_event_date = _optional_str(data.get("latest_event_date"))
    if latest_event_date is None:
        dates = sorted(
            date_s for e in events
            if isinstance(e, dict) and (date_s := _optional_str(e.get("date")))
        )
        latest_event_date = dates[-1] if dates else None

    latest_discovered_event_date = _optional_str(data.get("latest_discovered_event_date"))
    if latest_discovered_event_date is None:
        latest_discovered_event_date = latest_event_date

    scrape_horizon_date = _optional_str(data.get("scrape_horizon_date"))
    covers_horizon = data.get("covers_horizon")
    if not isinstance(covers_horizon, bool):
        coverage_latest = max(
            [d for d in (latest_event_date, latest_discovered_event_date) if isinstance(d, str)],
            default=None,
        )
        covers_horizon = (
            bool(coverage_latest and scrape_horizon_date and coverage_latest >= scrape_horizon_date)
            if scrape_horizon_date else None
        )

    loose = data.get("total_events_discovered")
    strict = data.get("total_events_discovered_strict")
    visible = data.get("total_events_visible")
    ref = _load_reference_total(slug)

    flags: list[str] = []
    if data.get("error"):
        flags.append("ERROR")
    if extracted == 0:
        flags.append("NO_EVENTS")
    if with_program < extracted:
        flags.append("MISSING_PROGRAM")
    if with_date < extracted:
        flags.append("MISSING_DATE")
    if covers_horizon is False:
        flags.append("SHORT_HORIZON")

    cov = VenueCoverage(
        slug=slug,
        venue=str(data.get("venue") or slug),
        source_url=str(data.get("source_url") or ""),
        scraped_at=str(data.get("scraped_at") or ""),
        engine=str(data.get("engine") or "unknown"),
        error=data.get("error"),
        extracted=extracted,
        visible=visible if isinstance(visible, int) else None,
        discovered_loose=loose if isinstance(loose, int) else None,
        discovered_strict=strict if isinstance(strict, int) else None,
        reference_total=ref,
        with_program=with_program,
        with_date=with_date,
        with_detail_url=with_detail,
        classical=classical,
        classical_with_program=classical_with_program,
        enriched=enriched,
        nonclassical_enriched=nonclassical_enriched,
        discovered_only=discovered_only,
        scrape_horizon_date=scrape_horizon_date,
        latest_event_date=latest_event_date,
        latest_discovered_event_date=latest_discovered_event_date,
        covers_horizon=covers_horizon,
        flags=flags,
    )

    ratio = cov.noise_ratio_loose
    if cov.discovered_loose is not None and cov.discovered_loose >= MAP_ABSOLUTE_WARN:
        flags.append(f"MAP_OVERCOUNT({cov.discovered_loose})")
    elif ratio is not None and ratio >= NOISE_RATIO_WARN:
        flags.append(f"HIGH_MAP_NOISE×{ratio:.0f}")

    if ref and cov.discovered_strict is not None:
        drift = abs(cov.discovered_strict - ref) / max(ref, 1)
        if drift > 0.5:
            flags.append(f"STRICT_vs_LEGACY±{int(drift * 100)}%")

    if ref and cov.extracted < min(5, ref) and extracted > 0:
        flags.append("SMOKE_ONLY")

    return cov


def _fmt_int(n: int | None) -> str:
    return "—" if n is None else str(n)


def _fmt_pct(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.0f}%"


def _fmt_horizon(row: VenueCoverage) -> str:
    if not row.scrape_horizon_date:
        return "—"
    marker = "✓" if row.covers_horizon else "!"
    return f"{marker} {row.scrape_horizon_date}"


def _fmt_latest(row: VenueCoverage) -> str:
    latest = row.latest_event_date or "—"
    discovered = row.latest_discovered_event_date
    if discovered and discovered != row.latest_event_date:
        return f"{latest} / d:{discovered}"
    return latest


def build_markdown(rows: list[VenueCoverage], generated_at: str) -> str:
    lines = [
        "# Concert scrape coverage report",
        "",
        f"Generated: {generated_at}",
        "",
        "Columns:",
        "- **ext**: events in JSON (smoke limit usually 5)",
        "- **vis**: `total_events_visible` from listing scrape",
        "- **map↓**: `total_events_discovered` (loose URL filter — often inflated)",
        "- **map✓**: `total_events_discovered_strict` (detail-page patterns)",
        "- **ref**: legacy custom scraper total (if available)",
        "- **prog**: share of extracted events with non-empty program",
        "- **last**: latest dated event in JSON; `d:` suffix means latest discovered date",
        "- **hzn**: configured scrape horizon; ✓ means discovery reaches it",
        "",
        "| Venue | ext | vis | map↓ | map✓ | ref | prog | last | hzn | flags |",
        "|-------|-----|-----|------|------|-----|------|------|-----|-------|",
    ]

    for r in sorted(rows, key=lambda x: x.venue.lower()):
        flag_s = ", ".join(r.flags) if r.flags else "—"
        lines.append(
            f"| {r.venue} | {r.extracted} | {_fmt_int(r.visible)} | "
            f"{_fmt_int(r.discovered_loose)} | {_fmt_int(r.discovered_strict)} | "
            f"{_fmt_int(r.reference_total)} | {_fmt_pct(r.program_rate)} | "
            f"{_fmt_latest(r)} | {_fmt_horizon(r)} | {flag_s} |"
        )

    lines.extend([
        "",
        "## Audit Matrix",
        "",
        "| Venue | Discovery | Dates | Program | Map | Cost | Adapter | Next |",
        "|-------|-----------|-------|---------|-----|------|---------|------|",
    ])

    for r in sorted(rows, key=lambda x: x.venue.lower()):
        adapter = "yes" if r.needs_adapter else "no"
        lines.append(
            f"| {r.venue} | {r.discovery_status} | {r.dates_status} "
            f"({r.with_date}/{r.extracted}) | {r.program_status} "
            f"({_fmt_pct(r.classical_program_rate)} cls) | {r.map_status} | "
            f"{r.cost_status} ({r.nonclassical_enriched}/{r.enriched}) | "
            f"{adapter} | {r.next_action} |"
        )

    lines.extend([
        "",
        "## Notes",
        "",
        "- **HIGH_MAP_NOISE**: `map()` loose count ≫ visible/extracted — do not use for "
        "full enrichment (cost explosion). Prefer **map✓** or listing-only metrics.",
        "- Firecrawl smoke separates discovery from enrichment: `max_events` is the "
        "detail-page enrichment budget, while discovered URL/stub coverage can be larger.",
        "- Discovery over-count usually comes from broad `/konzerte/` / `/programm/` "
        "paths in site-wide `map()`, not from listing extraction.",
        "- **SHORT_HORIZON**: latest event in the JSON is before the configured scrape horizon.",
        "- **Program** in the audit uses the programme rate for events classified as classical, "
        "not the raw all-event programme rate.",
        "- **Cost** warns when many enriched detail pages are clearly non-classical, because "
        "that burns the limited Firecrawl detail budget.",
        "",
    ])
    return "\n".join(lines)


def build_json(rows: list[VenueCoverage], generated_at: str) -> dict:
    return {
        "generated_at": generated_at,
        "venues": [
            {
                "slug": r.slug,
                "venue": r.venue,
                "extracted": r.extracted,
                "visible": r.visible,
                "discovered_loose": r.discovered_loose,
                "discovered_strict": r.discovered_strict,
                "reference_total": r.reference_total,
                "program_rate": r.program_rate,
                "classical": r.classical,
                "classical_program_rate": r.classical_program_rate,
                "enriched": r.enriched,
                "nonclassical_enriched": r.nonclassical_enriched,
                "discovered_only": r.discovered_only,
                "latest_event_date": r.latest_event_date,
                "latest_discovered_event_date": r.latest_discovered_event_date,
                "scrape_horizon_date": r.scrape_horizon_date,
                "covers_horizon": r.covers_horizon,
                "noise_ratio_loose": r.noise_ratio_loose,
                "noise_ratio_strict": r.noise_ratio_strict,
                "audit": {
                    "discovery": r.discovery_status,
                    "dates": r.dates_status,
                    "program": r.program_status,
                    "map": r.map_status,
                    "cost": r.cost_status,
                    "needs_adapter": r.needs_adapter,
                    "next_action": r.next_action,
                },
                "flags": r.flags,
                "error": r.error,
                "scraped_at": r.scraped_at,
            }
            for r in rows
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Coverage report for venue scrape JSON")
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help=f"Glob under backend/ (default: {DEFAULT_GLOB})",
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="Write markdown report to this path (e.g. reports/coverage.md)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Optional path for machine-readable JSON summary",
    )
    args = parser.parse_args()

    paths = sorted(BACKEND.glob(args.glob))
    if not paths:
        print(f"No files matching backend/{args.glob}", flush=True)
        raise SystemExit(1)

    rows = [_analyze_file(p) for p in paths]
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = build_markdown(rows, generated_at)

    print(md)

    if args.write:
        out = args.write if args.write.is_absolute() else REPO_ROOT / args.write
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"\nWrote {out}", flush=True)

    if args.json:
        jout = args.json if args.json.is_absolute() else REPO_ROOT / args.json
        jout.parent.mkdir(parents=True, exist_ok=True)
        jout.write_text(
            json.dumps(build_json(rows, generated_at), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote {jout}", flush=True)


if __name__ == "__main__":
    main()
