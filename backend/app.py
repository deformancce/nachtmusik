"""
op.us FastAPI backend.

Loads the scraped JSON databases on startup and exposes a smart-search API
over composers, works, and concert events.

Endpoints:
  GET /                                  — info & stats
  GET /api/search?q=...                  — smart search (composers + works)
  GET /api/composer/{name}/works         — works by a single composer
  GET /api/work/performances?work_title=&composer=  — events performing a work
"""
from typing import Optional, List, Dict
from pathlib import Path
import json
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="op.us API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COMPOSERS: List[Dict] = []
WORKS: List[Dict] = []
EVENTS: List[Dict] = []


def _load_json(path: Path, key: str) -> List[Dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get(key, [])


def load_data() -> None:
    global COMPOSERS, WORKS, EVENTS
    base = Path(__file__).parent
    COMPOSERS = _load_json(base / "composers_klassika.json", "composers")
    WORKS = _load_json(base / "works_klassika.json", "works")

    # Events come from one of two scrapers — load whichever exists
    gewandhaus = _load_json(base / "gewandhaus_events.json", "events")
    venues = _load_json(base / "all_venues_events.json", "events")
    EVENTS = gewandhaus + venues

    print(f"Loaded {len(COMPOSERS)} composers")
    print(f"Loaded {len(WORKS)} works")
    print(f"Loaded {len(EVENTS)} events")


load_data()


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    return (text
            .replace("ä", "a")
            .replace("ö", "o")
            .replace("ü", "u")
            .replace("ß", "ss"))


def extract_number(text: str) -> Optional[int]:
    match = re.search(r"\b(\d+)\b", text or "")
    return int(match.group(1)) if match else None


def smart_search(query: str) -> Dict:
    """Search composers and works in a single pass, ranked by relevance.

    Examples:
      "brahms"          → Brahms + his works
      "brahms 1"        → Brahms 1. Symphonie / 1. Klavierkonzert
      "mozart requiem"  → Mozart's Requiem
      "requiem"         → all requiems
    """
    if not query or len(query) < 2:
        return {"composers": [], "works": []}

    q_norm = normalize_text(query)
    q_parts = q_norm.split()
    q_number = extract_number(query)

    matching_composers = []
    for composer in COMPOSERS:
        name_norm = normalize_text(composer.get("name", ""))
        if q_norm in name_norm:
            relevance = 100 if q_norm == name_norm.split(",")[0] else 80
            matching_composers.append({**composer, "type": "composer", "relevance": relevance})

    matching_works = []
    for work in WORKS:
        title_norm = normalize_text(work.get("title", ""))
        composer_norm = normalize_text(work.get("composer", ""))
        opus_norm = normalize_text(work.get("opus", ""))

        composer_match = any(part in composer_norm for part in q_parts)
        work_match = any(part in title_norm for part in q_parts)

        relevance = 0
        if composer_match and work_match:
            relevance = 100
        elif composer_match and q_number is not None:
            num = str(q_number)
            if (num in title_norm
                    or f"nr. {num}" in title_norm
                    or f"no. {num}" in title_norm):
                relevance = 95
        elif work_match:
            relevance = 70
        elif q_norm in opus_norm:
            relevance = 60

        if relevance > 0:
            matching_works.append({**work, "type": "work", "relevance": relevance})

    matching_composers.sort(key=lambda x: x["relevance"], reverse=True)
    matching_works.sort(key=lambda x: x["relevance"], reverse=True)

    return {
        "composers": matching_composers[:10],
        "works": matching_works[:20],
    }


def find_matching_events(work_title: str, composer_name: str) -> List[Dict]:
    """Find events that appear to perform a given work.

    Matching strategy:
      - composer surname AND a meaningful work keyword in event title  → high
      - 2+ work keywords in event title                                → medium
      - work title appears in event.program list                       → high
    """
    matches: List[Dict] = []
    work_norm = normalize_text(work_title)
    composer_surname = normalize_text(composer_name).split(",")[0].strip()

    # Keep only meaningful keywords (drop opus/catalog markers, short words)
    work_keywords = [
        w for w in work_norm.split()
        if len(w) > 3 and not w.startswith(("op", "kv", "bwv", "wab", "hob"))
    ]

    for event in EVENTS:
        event_title = normalize_text(event.get("title", ""))
        event_program = " ".join(
            normalize_text(p) for p in event.get("program", []) if isinstance(p, str)
        )

        # Direct hit in program list
        if work_norm and work_norm in event_program:
            matches.append({**event, "confidence": "high",
                            "matched_keywords": [work_norm]})
            continue

        composer_match = bool(composer_surname) and composer_surname in event_title
        title_keyword_matches = [kw for kw in work_keywords if kw in event_title]

        if composer_match and title_keyword_matches:
            matches.append({**event, "confidence": "high",
                            "matched_keywords": title_keyword_matches})
        elif len(title_keyword_matches) >= 2:
            matches.append({**event, "confidence": "medium",
                            "matched_keywords": title_keyword_matches})

    return matches


@app.get("/")
def root():
    return {
        "message": "op.us API",
        "composers": len(COMPOSERS),
        "works": len(WORKS),
        "events": len(EVENTS),
        "examples": [
            "/api/search?q=brahms 1",
            "/api/search?q=mozart requiem",
            "/api/composer/Bach, Johann Sebastian/works",
            "/api/work/performances?work_title=Symphonie Nr. 9&composer=Beethoven",
        ],
    }


@app.get("/api/search")
def search(q: str):
    results = smart_search(q)
    return {
        "query": q,
        "total_composers": len(results["composers"]),
        "total_works": len(results["works"]),
        "composers": results["composers"],
        "works": results["works"],
    }


@app.get("/api/composer/{composer_name}/works")
def get_composer_works(composer_name: str):
    composer = next(
        (c for c in COMPOSERS if c.get("name", "").lower() == composer_name.lower()),
        None,
    )
    if not composer:
        return {"error": "Komponist nicht gefunden", "works": []}

    works = [
        w for w in WORKS
        if w.get("composer", "").lower() == composer["name"].lower()
    ]
    return {
        "composer": composer,
        "total_works": len(works),
        "works": works,
    }


@app.get("/api/work/performances")
def get_work_performances(work_title: str, composer: str):
    events = find_matching_events(work_title, composer)
    return {
        "work": work_title,
        "composer": composer,
        "total_performances": len(events),
        "performances": events,
    }
