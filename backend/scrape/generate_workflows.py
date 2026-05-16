"""
Workflow generator — generates .github/workflows/scrape-<slug>.yml for each
config in backend/scrape/configs/.

Also generates scrape-all-tier1.yml that runs all Tier-1 venues sequentially.

Usage:
    python3 -m backend.scrape.generate_workflows
    python3 -m backend.scrape.generate_workflows --only konzerthaus_berlin
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
WORKFLOWS_DIR = BASE.parent / ".github" / "workflows"
CONFIGS_DIR = BASE / "scrape" / "configs"

_WORKFLOW_TEMPLATE = """\
name: Scrape {name}

on:
  workflow_dispatch:
    inputs:
      branch:
        description: "Branch to scrape from and commit to"
        required: false
        default: "main"

permissions:
  contents: write

concurrency:
  group: scrape-{slug_dash}
  cancel-in-progress: false

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ github.event.inputs.branch || 'main' }}}}

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install --quiet crawl4ai pydantic anthropic fastapi
          python3 -m playwright install --with-deps chromium

      - name: Run scraper
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          PYTHONUNBUFFERED: "1"
        run: python3 -m backend.scrape.run_scraper {slug}

      - name: Show summary
        run: |
          python3 -c "
          import json
          with open('backend/{slug}_events.json') as f:
              data = json.load(f)
          total = data.get('total_events', 0)
          print(f'Total events: {{total}}')
          for e in data.get('events', [])[:5]:
              print(f\"  {{e.get('date','')}}: {{e.get('title','')[:60]}}\")
          "

      - name: Commit and push if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add backend/{slug}_events.json
          if git diff --cached --quiet; then
            echo "No changes to commit."
            exit 0
          fi
          git commit -m "Refresh {slug}_events.json (manual scrape)"
          BRANCH="${{GITHUB_REF_NAME:-main}}"
          for i in 1 2 3; do
            git pull --rebase --autostash origin "$BRANCH" || true
            if git push origin "HEAD:$BRANCH"; then exit 0; fi
            sleep $((i * 3))
          done
          exit 1
"""

_ALL_TIER1_TEMPLATE = """\
name: Scrape all Tier-1 venues

on:
  workflow_dispatch:
    inputs:
      branch:
        description: "Branch to scrape from and commit to"
        required: false
        default: "main"

permissions:
  contents: write

concurrency:
  group: scrape-all-tier1
  cancel-in-progress: false

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.inputs.branch || 'main' }}

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install --quiet crawl4ai pydantic anthropic fastapi
          python3 -m playwright install --with-deps chromium

      - name: Run all Tier-1 scrapers
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PYTHONUNBUFFERED: "1"
        run: python3 -m backend.scrape.run_scraper --all-tier 1

      - name: Commit and push results
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add backend/*_events.json
          if git diff --cached --quiet; then
            echo "No changes to commit."
            exit 0
          fi
          git commit -m "Refresh Tier-1 events (manual scrape run)"
          BRANCH="${GITHUB_REF_NAME:-main}"
          for i in 1 2 3; do
            git pull --rebase --autostash origin "$BRANCH" || true
            if git push origin "HEAD:$BRANCH"; then exit 0; fi
            sleep $((i * 3))
          done
          exit 1
"""


def generate_workflow(slug: str, name: str, force: bool) -> None:
    slug_dash = slug.replace("_", "-")
    out = WORKFLOWS_DIR / f"scrape-{slug_dash}.yml"
    if out.exists() and not force:
        print(f"  SKIP {out.name} (already exists; use --force)")
        return
    out.write_text(_WORKFLOW_TEMPLATE.format(slug=slug, slug_dash=slug_dash, name=name))
    print(f"  WRITE {out.name}")


def main(only: list[str] | None, force: bool) -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    config_files = sorted(CONFIGS_DIR.glob("*.py"))
    config_files = [f for f in config_files if f.name != "__init__.py"]

    slugs = []
    for f in config_files:
        slug = f.stem
        if only and slug not in only:
            continue
        try:
            mod = importlib.import_module(f"backend.scrape.configs.{slug}")
            name = mod.CONFIG.name
        except Exception:
            name = slug.replace("_", " ").title()
        generate_workflow(slug, name, force)
        slugs.append(slug)

    # Generate the all-tier-1 workflow
    all_out = WORKFLOWS_DIR / "scrape-all-tier1.yml"
    if not all_out.exists() or force:
        all_out.write_text(_ALL_TIER1_TEMPLATE)
        print(f"  WRITE {all_out.name}")
    else:
        print(f"  SKIP {all_out.name}")

    print(f"Done. Generated workflows for: {', '.join(slugs) or '(none)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="SLUG")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    main(only=args.only, force=args.force)
