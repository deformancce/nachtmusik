#!/usr/bin/env bash
# Run Firecrawl Tier-0 smoke scrape locally.
# Usage:
#   cp .env.example .env   # add FIRECRAWL_API_KEY=fc-...
#   ./scripts/run_firecrawl_smoke.sh
#   ./scripts/run_firecrawl_smoke.sh konzerthaus_berlin elbphilharmonie_hamburg
#   MODE=probe ./scripts/run_firecrawl_smoke.sh elbphilharmonie_hamburg
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

pip3 install -q --upgrade firecrawl-py==4.27.1 pydantic requests beautifulsoup4 2>/dev/null || true

ARGS=(--max-events "${MAX_EVENTS:-5}" --horizon-months "${SCRAPE_HORIZON_MONTHS:-6}" --skip-map)
if [[ "${MODE:-scrape}" == "probe" ]]; then
  ARGS+=(--probe-discovery)
fi
if [[ $# -gt 0 ]]; then
  ARGS+=(--only "$@")
fi

echo "Running Firecrawl ${MODE:-scrape} (${#ARGS[@]} args)..."
python3 -m backend.scrape.firecrawl_scraper "${ARGS[@]}"
