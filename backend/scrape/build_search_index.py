"""
Build the Meilisearch works index from event programs + work_popularity.json.

Usage:
    python3 -m backend.scrape.build_search_index

Requires Meilisearch running at MEILI_URL (default http://localhost:7700).
Start it with: docker compose up -d meilisearch
"""
import json
import os
import re
import sys
from pathlib import Path
from collections import Counter

BASE = Path(__file__).parent.parent

MEILI_URL = os.getenv("MEILI_URL", "http://localhost:7700")
MEILI_KEY = os.getenv("MEILI_MASTER_KEY", "")

_GENRE_DEFAULTS = {}
_WORK_OVERRIDES = {}


def load_popularity() -> None:
    p = BASE / "data" / "work_popularity.json"
    if not p.exists():
        return
    data = json.loads(p.read_text())
    _GENRE_DEFAULTS.update(data.get("_genre_defaults", {}))
    _WORK_OVERRIDES.update(data.get("works", {}))


def detect_genre_popularity(title: str) -> int:
    title_l = title.lower().replace("ü", "u").replace("ä", "a").replace("ö", "o")
    for keyword, score in _GENRE_DEFAULTS.items():
        if keyword in title_l:
            return score
    return 30


def lookup_work_popularity(composer: str, title: str) -> int | None:
    # Try substring-match on the override keys (e.g. "Brahms|Symphonie Nr. 1")
    c_short = composer.split(",")[0].strip()
    for key, score in _WORK_OVERRIDES.items():
        k_composer, _, k_title = key.partition("|")
        if k_composer.lower() in composer.lower() or composer.lower() in k_composer.lower():
            if k_title.lower() in title.lower() or title.lower() in k_title.lower():
                return score
    return None


def build_works_from_events(events: list[dict]) -> list[dict]:
    work_counts: Counter = Counter()
    for ev in events:
        for entry in ev.get("program") or []:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            composer, _, work = entry.partition(":")
            composer, work = composer.strip(), work.strip()
            if composer and work:
                work_counts[(composer, work)] += 1

    docs = []
    for i, ((composer, title), count) in enumerate(work_counts.most_common()):
        override = lookup_work_popularity(composer, title)
        popularity = override if override is not None else detect_genre_popularity(title)
        docs.append({
            "id": str(i),
            "composer": composer,
            "title": title,
            "count": count,
            "popularity": popularity,
        })
    return docs


def load_events() -> list[dict]:
    events = []
    for f in BASE.glob("*_events.json"):
        data = json.loads(f.read_text())
        if isinstance(data, list):
            events.extend(data)
        else:
            events.extend(data.get("events", []))
    return events


def push_to_meilisearch(docs: list[dict]) -> None:
    try:
        import meilisearch
    except ImportError:
        print("meilisearch package not installed. Run: pip install meilisearch")
        sys.exit(1)

    client = meilisearch.Client(MEILI_URL, MEILI_KEY or None)
    index = client.index("works")

    # Configure ranking and filterable attributes
    index.update_settings({
        "rankingRules": [
            "words",
            "typo",
            "proximity",
            "attribute",
            "popularity:desc",
            "exactness",
        ],
        "filterableAttributes": ["composer"],
        "sortableAttributes": ["popularity"],
        "searchableAttributes": ["composer", "title"],
        "typoTolerance": {
            "enabled": True,
            "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8},
        },
        "synonyms": {
            "brahms 1": ["brahms symphonie 1", "brahms erste symphonie"],
            "mahler 9": ["mahler symphonie 9", "mahler neunte"],
        },
    })

    index.add_documents(docs, primary_key="id")
    print(f"Pushed {len(docs)} works to Meilisearch index 'works' at {MEILI_URL}")


if __name__ == "__main__":
    load_popularity()
    events = load_events()
    docs = build_works_from_events(events)
    print(f"Built {len(docs)} unique works from {len(events)} events")
    push_to_meilisearch(docs)
