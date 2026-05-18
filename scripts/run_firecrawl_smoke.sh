#!/usr/bin/env bash
# Run Firecrawl Tier-0 smoke scrape locally.
# Usage:
#   cp .env.example .env   # add FIRECRAWL_API_KEY=fc-...
#   ./scripts/run_firecrawl_smoke.sh
#   ./scripts/run_firecrawl_smoke.sh konzerthaus_berlin elbphilharmonie_hamburg
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${FIRECRAWL_API_KEY:-}" ]]; then
  echo "ERROR: FIRECRAWL_API_KEY not set. Add it to .env or export it." >&2
  exit 1
fi

pip3 install -q firecrawl-py pydantic 2>/dev/null || true

ARGS=(--max-events 5 --horizon-months "${SCRAPE_HORIZON_MONTHS:-6}")
if [[ $# -gt 0 ]]; then
  ARGS+=(--only "$@")
fi

echo "Running Firecrawl smoke (${#ARGS[@]} args)..."
python3 -m backend.scrape.firecrawl_scraper "${ARGS[@]}"
