# Concert scrape coverage report

Generated: 2026-05-17 13:38 UTC

Columns:
- **ext**: events in JSON (smoke limit usually 5)
- **vis**: `total_events_visible` from listing scrape
- **map↓**: `total_events_discovered` (loose URL filter — often inflated)
- **map✓**: `total_events_discovered_strict` (detail-page patterns)
- **ref**: legacy custom scraper total (if available)
- **prog**: share of extracted events with non-empty program

| Venue | ext | vis | map↓ | map✓ | ref | prog | flags |
|-------|-----|-----|------|------|-----|------|-------|
| Alte Oper Frankfurt | 5 | 20 | 20 | 20 | — | 60% | MISSING_PROGRAM |
| Berliner Philharmonie | 5 | 5 | 5 | 5 | 329 | 100% | STRICT_vs_LEGACY±98% |
| Elbphilharmonie Hamburg | 5 | 10 | 10 | 10 | — | 80% | MISSING_PROGRAM |
| Festspielhaus Baden-Baden | 5 | 39 | 39 | 39 | — | 60% | MISSING_PROGRAM |
| Gewandhaus Leipzig | 5 | 5 | 5 | 5 | 366 | 60% | MISSING_PROGRAM, STRICT_vs_LEGACY±98% |
| Glocke Bremen | 5 | 12 | 12 | 12 | — | 80% | MISSING_PROGRAM |
| Isarphilharmonie München | 5 | 30 | 30 | 30 | — | 100% | — |
| Konzerthaus Berlin | 5 | 49 | 49 | 49 | — | 100% | — |
| Konzerthaus Dortmund | 5 | 17 | 17 | 17 | — | 60% | MISSING_PROGRAM |
| Kölner Philharmonie | 5 | 50 | 50 | 50 | — | 100% | — |
| Liederhalle Stuttgart | 5 | 17 | 17 | 17 | — | 40% | MISSING_PROGRAM |
| Philharmonie Essen | 5 | 18 | 18 | 18 | — | 60% | MISSING_PROGRAM |
| Tonhalle Düsseldorf | 5 | 21 | 21 | 21 | — | 80% | MISSING_PROGRAM |

## Notes

- **HIGH_MAP_NOISE**: `map()` loose count ≫ visible/extracted — do not use for full enrichment (cost explosion). Prefer **map✓** or listing-only metrics.
- Firecrawl smoke uses ~7 credits/venue (1 map + 1 listing + 5 details). Use `--skip-map` to save 1 credit/venue.
- Discovery over-count usually comes from broad `/konzerte/` / `/programm/` paths in site-wide `map()`, not from listing extraction.
