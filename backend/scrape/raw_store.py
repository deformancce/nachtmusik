"""
Persist raw Firecrawl markdown so we can reprocess without spending credits.

Files land at backend/raw/<slug>/<UTC-timestamp>.md with a short URL header.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional


RAW_DIR = Path(__file__).parent.parent / "raw"


def save_markdown(slug: str, url: str, markdown: str) -> Optional[Path]:
    if not markdown or not markdown.strip():
        return None
    venue_dir = RAW_DIR / slug
    venue_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    out = venue_dir / f"{stamp}.md"
    header = f"<!-- url: {url} -->\n<!-- fetched: {stamp} -->\n\n"
    out.write_text(header + markdown, encoding="utf-8")
    return out
