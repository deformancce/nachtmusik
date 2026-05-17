# Concert scrape coverage report

Generated: 2026-05-17 13:13 UTC

Columns:
- **ext**: events in JSON (smoke limit usually 5)
- **vis**: `total_events_visible` from listing scrape
- **map↓**: `total_events_discovered` (loose URL filter — often inflated)
- **map✓**: `total_events_discovered_strict` (detail-page patterns)
- **ref**: legacy custom scraper total (if available)
- **prog**: share of extracted events with non-empty program

| Venue | ext | vis | map↓ | map✓ | ref | prog | flags |
|-------|-----|-----|------|------|-----|------|-------|
| Alte Oper Frankfurt | 5 | 10 | 97 | — | — | 100% | HIGH_MAP_NOISE×10 |
| Berliner Philharmonie | 5 | 5 | 1588 | — | 329 | 60% | MISSING_PROGRAM, MAP_OVERCOUNT(1588) |
| Elbphilharmonie Hamburg | 5 | 8 | 106 | — | — | 80% | MISSING_PROGRAM, HIGH_MAP_NOISE×13 |
| Festspielhaus Baden-Baden | 5 | 23 | 23 | — | — | 40% | MISSING_PROGRAM |
| Gewandhaus Leipzig | 5 | 5 | 710 | — | 366 | 60% | MISSING_PROGRAM, MAP_OVERCOUNT(710) |
| Glocke Bremen | 5 | 14 | 14 | — | — | 60% | MISSING_PROGRAM |
| Isarphilharmonie München | 2 | 36 | 36 | — | — | 100% | — |
| Konzerthaus Berlin | 5 | 30 | 750 | — | — | 100% | MAP_OVERCOUNT(750) |
| Konzerthaus Dortmund | 1 | 40 | 95 | — | — | 100% | — |
| Kölner Philharmonie | 5 | 479 | 4056 | — | — | 100% | MAP_OVERCOUNT(4056) |
| Liederhalle Stuttgart | 5 | 15 | 15 | — | — | 40% | MISSING_PROGRAM |
| Philharmonie Essen | 5 | 12 | 4 | — | — | 40% | MISSING_PROGRAM |
| Tonhalle Düsseldorf | 5 | 66 | 1 | — | — | 80% | MISSING_PROGRAM |

## Notes

- **HIGH_MAP_NOISE**: `map()` loose count ≫ visible/extracted — do not use for full enrichment (cost explosion). Prefer **map✓** or listing-only metrics.
- Firecrawl smoke uses ~7 credits/venue (1 map + 1 listing + 5 details). Use `--skip-map` to save 1 credit/venue.
- Discovery over-count usually comes from broad `/konzerte/` / `/programm/` paths in site-wide `map()`, not from listing extraction.
