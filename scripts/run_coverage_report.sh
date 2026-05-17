#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m backend.monitoring.coverage_report --write reports/coverage.md --json reports/coverage.json "$@"
