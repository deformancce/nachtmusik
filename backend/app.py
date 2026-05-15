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

    # Each scraper writes to its own venue-named file.
    venue_files = [
        "gewandhaus_events.json",
        "berliner_philharmonie_events.json",
        # add more as we onboard venues
    ]

    # Dedup by (venue, id) so the Gewandhaus's "9588" and the Berliner's
    # "56430" don't collide; same event reposted by the same venue collapses.
    seen: set = set()
    merged: List[Dict] = []
    for fname in venue_files:
        for ev in _load_json(base / fname, "events"):
            key = (ev.get("venue", ""), ev.get("id", ""))
            if key in seen:
                continue
            if ev.get("id"):
                seen.add(key)
            merged.append(ev)
    EVENTS = merged

    # Sort by date ascending, undated events last
    EVENTS.sort(key=lambda e: e.get("date") or "9999-99-99")

    print(f"Loaded {len(COMPOSERS)} composers")
    print(f"Loaded {len(WORKS)} works")
    print(f"Loaded {len(EVENTS)} events from {len(venue_files)} venues")


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


# Genre keywords used for fuzzy work matching. Each value lists the variants
# (German, English, abbreviations) that count as "the same thing" so the user
# can search "symphony 3" or "sinfonie 3" or "symphonie nr. 3" interchangeably.
_GENRE_ALIASES: Dict[str, List[str]] = {
    "symphonie":     ["symphonie", "sinfonie", "symphony"],
    "klavierkonzert": ["klavierkonzert", "piano concerto", "konzert für klavier"],
    "violinkonzert":  ["violinkonzert", "violin concerto", "konzert für violine"],
    "cellokonzert":   ["cellokonzert", "cello concerto", "konzert für violoncello"],
    "konzert":       ["konzert", "concerto"],  # generic concerto, lower priority
    "streichquartett": ["streichquartett", "string quartet"],
    "streichquintett": ["streichquintett", "string quintet"],
    "klaviersonate":  ["klaviersonate", "piano sonata", "sonate für klavier"],
    "messe":         ["messe", "mass"],
    "requiem":       ["requiem"],
    "oratorium":     ["oratorium", "oratorio"],
    "oper":          ["oper", "opera"],
    "ouvertüre":     ["ouvertüre", "ouverture", "overture"],
}

# Particles to drop from a composer's name when picking the surname.
_NAME_PARTICLES = {"sir", "dr", "dr.", "von", "van", "de", "der", "von_der"}


def composer_surname(name: str) -> str:
    """Take the last meaningful token of a composer name.
    'Gustav Mahler' → 'mahler'; 'Johann Sebastian Bach' → 'bach';
    'Sergej Rachmaninoff' → 'rachmaninoff'."""
    parts = [p for p in normalize_text(name).split() if p and p not in _NAME_PARTICLES]
    return parts[-1] if parts else ""


def parse_program_entry(entry: str) -> tuple:
    """Split a 'Composer: Werk' program string into (composer, work).
    Falls back to ('', entry) when there's no colon."""
    if ":" in entry:
        composer, _, work = entry.partition(":")
        return composer.strip(), work.strip()
    return "", entry.strip()


def extract_work_signature(query: str) -> Dict:
    """Turn a free-text work query into a structured signature for matching.

    'Symphonie Nr. 3'    → {'genre': 'symphonie', 'number': 3, 'keywords': []}
    'Mahler 3'           → {'genre': None,        'number': 3, 'keywords': []}
    'Carmen'             → {'genre': None,        'number': None, 'keywords': ['carmen']}
    'Klavierkonzert Nr. 2 d-moll' → {'genre': 'klavierkonzert', 'number': 2, 'keywords': ['d-moll']}
    """
    text = normalize_text(query)
    sig: Dict = {"raw": text, "genre": None, "number": None, "keywords": []}

    # Genre: longest-match wins so "klavierkonzert" beats the generic "konzert"
    for genre, aliases in sorted(_GENRE_ALIASES.items(), key=lambda kv: -max(len(a) for a in kv[1])):
        if any(a in text for a in aliases):
            sig["genre"] = genre
            break

    # Number — tolerate "nr. 3", "no. 3", "3.", "#3", or just "3"
    num_match = re.search(r"(?:nr\.?\s*|no\.?\s*|#\s*)?(\d+)\.?", text)
    if num_match:
        # Reject if the number is preceded by an opus marker (op./kv./bwv/...)
        start = num_match.start(1)
        prefix = text[max(0, start - 6):start]
        if not re.search(r"(op\.?\s*|kv\.?\s*|bwv\s*|wab\s*|hob\.?\s*|d\.?\s*)$", prefix):
            sig["number"] = int(num_match.group(1))

    # Other keywords (drop genre aliases, the number itself, and short words)
    drop = {a for aliases in _GENRE_ALIASES.values() for a in aliases}
    if sig["number"] is not None:
        drop.add(str(sig["number"]))
    sig["keywords"] = [
        w for w in text.split()
        if len(w) > 3 and w not in drop and not re.match(r"^(nr|no)\.?$", w)
    ]
    return sig


def work_text_matches(work_text: str, sig: Dict) -> bool:
    """Does this 'Composer: Work' work-half match the signature?"""
    text = normalize_text(work_text)

    if sig["genre"]:
        if not any(a in text for a in _GENRE_ALIASES[sig["genre"]]):
            return False

    if sig["number"] is not None:
        # Number must appear as a standalone token, not inside an opus/catalog
        # number that happens to contain it.
        n = sig["number"]
        for m in re.finditer(rf"\b{n}\b\.?", text):
            prefix = text[max(0, m.start() - 6):m.start()]
            if not re.search(r"(op\.?\s*|kv\.?\s*|bwv\s*|wab\s*|hob\.?\s*|d\.?\s*)$", prefix):
                break
        else:
            return False

    # If neither genre nor number is set, every keyword must appear.
    if sig["genre"] is None and sig["number"] is None:
        if not sig["keywords"]:
            return False
        if not all(kw in text for kw in sig["keywords"]):
            return False

    return True


def find_matching_events(work_title: str, composer_name: str) -> List[Dict]:
    """Find events whose program lists a work matching the query.

    Matching is structured:
      - composer surname must equal the surname of the program-entry composer
      - work half must satisfy the work signature (genre, number, or keywords)

    Returns a list with the matched program entry attached so the caller can
    show *what* matched (helpful when an event has multiple works)."""
    sig = extract_work_signature(work_title)
    target_surname = composer_surname(composer_name)
    matches: List[Dict] = []

    for event in EVENTS:
        for entry in (event.get("program") or []):
            if not isinstance(entry, str):
                continue
            entry_composer, entry_work = parse_program_entry(entry)
            if target_surname and composer_surname(entry_composer) != target_surname:
                continue
            if not work_text_matches(entry_work, sig):
                continue
            matches.append({
                **event,
                "matched_program_entry": entry,
                "confidence": "high",
            })
            break  # one matched work per event is enough

    return matches


@app.get("/")
def root():
    venues = sorted({e.get("venue", "") for e in EVENTS if e.get("venue")})
    return {
        "message": "op.us API",
        "composers": len(COMPOSERS),
        "works": len(WORKS),
        "events": len(EVENTS),
        "venues": venues,
        "examples": [
            "/api/search?q=brahms 1",
            "/api/search?q=mozart requiem",
            "/api/composer/Bach, Johann Sebastian/works",
            "/api/work/performances?q=mahler+3",
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
def get_work_performances(
    work_title: Optional[str] = None,
    composer: Optional[str] = None,
    q: Optional[str] = None,
):
    """Find concerts performing a work.

    Two ways to call this:
      ?work_title=Symphonie+Nr.+3&composer=Mahler   (explicit)
      ?q=mahler+3                                   (auto-split: first word
                                                     is treated as composer
                                                     surname, rest as work)
    """
    if q and not work_title and not composer:
        # Naive split: first token is the composer surname, rest is the work.
        # Works for the common case ("mahler 3", "beethoven 9", "brahms requiem"),
        # falls back to all-keyword matching for ambiguous queries.
        parts = q.strip().split(None, 1)
        composer = parts[0] if parts else ""
        work_title = parts[1] if len(parts) > 1 else q
    work_title = work_title or ""
    composer = composer or ""
    events = find_matching_events(work_title, composer)
    return {
        "query": q,
        "work": work_title,
        "composer": composer,
        "total_performances": len(events),
        "performances": events,
    }


@app.get("/api/events")
def get_events(
    skip: int = 0,
    limit: int = 8,
    venue: Optional[str] = None,
    include_past: bool = False,
):
    """Paginated upcoming-concerts feed.

    - Past events (date < today UTC) are hidden by default; pass
      include_past=true to disable that filter.
    - venue=... narrows to a single venue (exact match on the venue field).
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    pool = EVENTS
    if not include_past:
        pool = [e for e in pool if (e.get("date") or "0000-00-00") >= today]
    if venue:
        pool = [e for e in pool if e.get("venue") == venue]

    total = len(pool)
    page = pool[skip : skip + limit]
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "venue": venue,
        "has_more": (skip + limit) < total,
        "events": page,
    }
