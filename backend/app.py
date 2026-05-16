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
import os
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

    # If the klassika data is missing (the JSONs are gitignored — a fresh
    # checkout doesn't have them), synthesize composers + works from what
    # the venues are actually programming. Suggestions stay grounded in
    # real upcoming concerts; the user types "Mahler" and only sees the
    # composers that have something on the schedule.
    if not COMPOSERS and not WORKS:
        COMPOSERS, WORKS = _derive_composers_and_works(EVENTS)
        print(f"  (klassika data missing — derived {len(COMPOSERS)} composers "
              f"and {len(WORKS)} works from {len(EVENTS)} events)")

    print(f"Loaded {len(COMPOSERS)} composers")
    print(f"Loaded {len(WORKS)} works")
    print(f"Loaded {len(EVENTS)} events from {len(venue_files)} venues")


def _derive_composers_and_works(events: List[Dict]) -> "tuple[List[Dict], List[Dict]]":
    """Build composer + work suggestion lists from events' program entries.

    Each program entry has the shape "Composer Name: Work Title (opus)".
    We split on the first colon, treat the LHS as the composer and the RHS
    as the work. Composers come back as {name, count} so the autocomplete
    can rank by how often a composer actually shows up on the schedule.
    Works come back as {composer, title, count} similarly.
    """
    from collections import Counter
    composer_counts: Counter = Counter()
    work_counts: Counter = Counter()  # key = (composer, work)
    for ev in events:
        for entry in ev.get("program") or []:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            composer, _, work = entry.partition(":")
            composer = composer.strip()
            work = work.strip()
            if not composer or not work:
                continue
            composer_counts[composer] += 1
            work_counts[(composer, work)] += 1

    composers = [
        {"name": name, "count": n}
        for name, n in composer_counts.most_common()
    ]
    works = [
        {"composer": c, "title": w, "count": n}
        for (c, w), n in work_counts.most_common()
    ]
    return composers, works


load_data()

# ── Popularity / genre-boost ─────────────────────────────────────────────────

_POPULARITY_DATA: Dict = {}

def _load_popularity() -> None:
    p = Path(__file__).parent / "data" / "work_popularity.json"
    if p.exists():
        _POPULARITY_DATA.update(json.loads(p.read_text()))

_load_popularity()

_GENRE_DEFAULTS: Dict[str, int] = _POPULARITY_DATA.get("_genre_defaults", {})
_WORK_OVERRIDES: Dict[str, int] = _POPULARITY_DATA.get("works", {})


def _work_genre_bonus(title: str) -> int:
    title_n = normalize_text(title) if title else ""
    for keyword, score in _GENRE_DEFAULTS.items():
        if keyword in title_n:
            return score
    return 30


def _work_popularity(composer: str, title: str) -> int:
    c_short = composer.split(",")[0].strip()
    for key, score in _WORK_OVERRIDES.items():
        k_composer, _, k_title = key.partition("|")
        if (k_composer.lower() in composer.lower()
                and k_title.lower() in title.lower()):
            return score
    return _work_genre_bonus(title)


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
        # Numbers must match as whole tokens to avoid "1" matching inside "51" or "91"
        def _part_in(part: str, text: str) -> bool:
            if re.match(r'^\d+$', part):
                return bool(re.search(rf'\b{part}\b', text))
            return part in text
        work_match = any(_part_in(part, title_norm) for part in q_parts)

        relevance = 0
        if composer_match and work_match:
            relevance = 1000
        elif composer_match and q_number is not None:
            num = str(q_number)
            if (bool(re.search(rf'\b{num}\b', title_norm))
                    or f"nr. {num}" in title_norm
                    or f"no. {num}" in title_norm):
                relevance = 950
        elif work_match:
            relevance = 700
        elif q_norm in opus_norm:
            relevance = 600

        if relevance > 0:
            # Add popularity as fine-grained tiebreaker (0-100) within each relevance band
            popularity = _work_popularity(work.get("composer", ""), work.get("title", ""))
            matching_works.append({
                **work,
                "type": "work",
                "relevance": relevance + popularity,
            })

    matching_composers.sort(key=lambda x: x["relevance"], reverse=True)
    matching_works.sort(key=lambda x: x["relevance"], reverse=True)

    return {
        "composers": matching_composers[:10],
        "works": matching_works[:20],
    }


# Genre keywords used for fuzzy work matching. Each value lists the variants
# (German, English, abbreviations) that count as "the same thing" so the user
# can search "symphony 3" or "sinfonie 3" or "symphonie nr. 3" interchangeably.
# Values are PRE-NORMALIZED at module load so comparisons against
# normalize_text(work_text) work even with German umlauts (ü→u etc.).
_GENRE_ALIASES_RAW: Dict[str, List[str]] = {
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
_GENRE_ALIASES: Dict[str, List[str]] = {
    key: [normalize_text(a) for a in aliases]
    for key, aliases in _GENRE_ALIASES_RAW.items()
}

# Particles to drop from a composer's name when picking the surname.
_NAME_PARTICLES = {"sir", "dr", "dr.", "von", "van", "de", "der", "von_der"}


# Composer name aliases — different sources use different transliterations
# for the same person (German vs. English vs. Russian transliteration).
# Values are the canonical form we normalize TO. Lowercased on lookup.
# Keys come from any source string; values are the "winner" used for matching.
_COMPOSER_ALIASES: Dict[str, str] = {
    # Rachmaninoff family
    "rachmaninow":   "rachmaninoff",
    "rachmaninov":   "rachmaninoff",
    "rachmaninoff":  "rachmaninoff",
    # Tchaikovsky family
    "tschaikowski":  "tschaikowski",
    "tschaikowsky":  "tschaikowski",
    "tchaikowski":   "tschaikowski",
    "tchaikovsky":   "tschaikowski",
    "tchaikowsky":   "tschaikowski",
    "chaikovskij":   "tschaikowski",
    # Shostakovich
    "schostakowitsch": "schostakowitsch",
    "shostakovich":    "schostakowitsch",
    "schostakovich":   "schostakowitsch",
    "shostakovich":    "schostakowitsch",
    # Stravinsky
    "strawinsky":   "strawinsky",
    "stravinsky":   "strawinsky",
    "strawinski":   "strawinsky",
    # Prokofiev
    "prokoffjew":   "prokoffjew",
    "prokofiev":    "prokoffjew",
    "prokofjew":    "prokoffjew",
    "prokofieff":   "prokoffjew",
    # Mussorgsky
    "mussorgski":   "mussorgski",
    "mussorgsky":   "mussorgski",
    "musorgskij":   "mussorgski",
    "mussorgskij":  "mussorgski",
    # Rimsky-Korsakov
    "rimski-korsakow": "rimski-korsakow",
    "rimsky-korsakov": "rimski-korsakow",
    "rimski-korsakov": "rimski-korsakow",
    # Schoenberg
    "schoenberg":   "schönberg",
    "schönberg":    "schönberg",
    # Dvořák
    "dvořák":       "dvorak",
    "dvorak":       "dvorak",
    # Sibelius — no aliasing needed
    # Bach (no transliteration variants, but multiple Bachs)
    # ...add more as we encounter them in the wild
}


def composer_surname(name: str) -> str:
    """Take the surname of a composer name. Handles both formats:
      - "Surname, First Middle"  (klassika style)
      - "First Middle Surname"   (event programme style)
    Returns the canonical surname form via _COMPOSER_ALIASES so that
    e.g. "Rachmaninow" and "Rachmaninoff" both collapse to the same key
    and match each other across data sources.
    """
    if "," in name:
        surname = normalize_text(name.split(",", 1)[0])
    else:
        parts = [p for p in normalize_text(name).split() if p and p not in _NAME_PARTICLES]
        surname = parts[-1] if parts else ""
    return _COMPOSER_ALIASES.get(surname, surname)


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


# Top-50 commonly-programmed composers — rough fame ranking used to bias
# autocomplete towards what users most likely want when they type just one
# letter. Lower-cased surnames; the order doesn't matter (membership test).
_FAMOUS_SURNAMES = frozenset({
    "bach", "beethoven", "mozart", "brahms", "schubert", "wagner", "mahler",
    "chopin", "tschaikowski", "tchaikovsky", "schumann", "haydn", "handel",
    "händel", "dvorak", "dvořák", "debussy", "ravel", "verdi", "puccini",
    "rachmaninoff", "rachmaninow", "sibelius", "mendelssohn", "bruckner",
    "schostakowitsch", "schostakovich", "shostakovich", "strawinsky",
    "stravinsky", "prokoffjew", "prokofiev", "liszt", "berlioz", "elgar",
    "fauré", "faure", "gershwin", "bartók", "bartok", "hindemith", "strauss",
    "rimsky-korsakov", "rimski-korsakow", "scarlatti", "purcell", "monteverdi",
    "vivaldi", "telemann", "buxtehude", "schütz", "gluck", "weber",
    "donizetti", "rossini", "bellini", "berg", "webern", "schönberg",
    "schoenberg", "messiaen", "ligeti", "pärt", "part",
})


def _famous_boost(name: str) -> int:
    # Tolerant of both "Surname, First" (klassika data) and "First Surname"
    # (composers derived from event program strings).
    if "," in name:
        surname = name.split(",")[0].strip().lower()
    else:
        parts = name.strip().split()
        surname = parts[-1].lower() if parts else ""
    return 30 if surname in _FAMOUS_SURNAMES else 0


_MEILI_URL = os.getenv("MEILI_URL", "")
_MEILI_KEY = os.getenv("MEILI_MASTER_KEY", "")
_meili_client = None

def _get_meili_client():
    global _meili_client
    if _meili_client is not None:
        return _meili_client
    if not _MEILI_URL:
        return None
    try:
        import meilisearch
        client = meilisearch.Client(_MEILI_URL, _MEILI_KEY or None)
        client.health()  # raises if not reachable
        _meili_client = client
        print(f"Meilisearch connected at {_MEILI_URL}")
    except Exception:
        _meili_client = None
    return _meili_client


def _meili_search(q: str) -> Dict | None:
    client = _get_meili_client()
    if client is None:
        return None
    try:
        result = client.index("works").search(q, {
            "limit": 20,
            "sort": ["popularity:desc"],
        })
        works = [
            {"composer": h["composer"], "title": h["title"],
             "type": "work", "relevance": h.get("popularity", 50)}
            for h in result.get("hits", [])
        ]
        return {"composers": [], "works": works}
    except Exception:
        return None


@app.get("/api/search")
def search(q: str):
    results = _meili_search(q) or smart_search(q)
    return {
        "query": q,
        "total_composers": len(results["composers"]),
        "total_works": len(results["works"]),
        "composers": results["composers"],
        "works": results["works"],
    }


@app.get("/api/autocomplete")
def autocomplete(q: str, limit: int = 8):
    """Live-suggestion endpoint for the search box.

    Ranks composer-name matches above work matches; well-known composers
    (Mahler, Bach, Beethoven, …) get a fame boost so a single-letter query
    surfaces the obvious household names first. Returns a flat list of
    {type, label, composer, work?, opus?} entries the frontend can render
    as a dropdown.

    Handles three input shapes:
      - short query "mahl"  → composer prefix-match
      - "mahler 3"          → composer + work token match
      - "Sergej Rachmaninoff: 2. Konzert ..." (paste from a programme
         listing) → split on ":", canonical-surname-match the composer
         half, token-match the work half. Aliases ensure Rachmaninoff /
         Rachmaninow / Rachmaninov all collapse to the same canonical key.
    """
    q = (q or "").strip()
    if len(q) < 1:
        return {"suggestions": []}

    # Detect paste-from-programme shape ("Composer: Werk") so we can split
    # and aliasing-match the composer side properly.
    pasted_composer = ""
    pasted_work_q = ""
    if ":" in q and len(q) > 20:
        head, _, tail = q.partition(":")
        if head.strip() and tail.strip():
            pasted_composer = head.strip()
            pasted_work_q = tail.strip()

    q_norm = normalize_text(q)
    q_parts = [p for p in q_norm.split() if p]
    if not q_parts:
        return {"suggestions": []}

    # Canonical surname of the query (for cross-transliteration matching).
    target_surname = composer_surname(pasted_composer or q)

    suggestions: List[Dict] = []

    # Composer matches — each composer surfaces at most once.
    for c in COMPOSERS:
        name = c.get("name", "")
        if not name:
            continue
        name_norm = normalize_text(name)
        c_surname = composer_surname(name)

        # Three ways to match:
        #   (a) every query token appears somewhere in the composer name
        #       — handles "mahl" and "bee" prefix typing
        #   (b) canonical surname equality — handles "Rachmaninoff" query
        #       finding "Rachmaninow, Sergei Wassiljewitsch"
        #   (c) when the user pasted a Composer: Werk string, the
        #       canonical surname of the head matches
        token_match = all(p in name_norm for p in q_parts)
        alias_match = bool(target_surname) and target_surname == c_surname
        if not (token_match or alias_match):
            continue
        score = 100 + _famous_boost(name)
        # Bigger boost when the surname starts with the query (typed-prefix).
        if "," in name_norm:
            surname_norm = name_norm.split(",")[0].strip()
        else:
            parts_n = name_norm.strip().split()
            surname_norm = parts_n[-1] if parts_n else ""
        if surname_norm.startswith(q_parts[0]):
            score += 40
        if alias_match and not token_match:
            # User typed a transliteration that doesn't substring-match —
            # still useful to suggest, but lower than a direct hit.
            score -= 20
        suggestions.append({
            "type": "composer",
            "label": name,
            "composer": name,
            "score": score,
        })

    # Work matches — every query part has to be findable somewhere in the
    # composer-name+work-title concatenation. Numbers like "3" match works
    # whose title contains "3" (Symphonie Nr. 3, Klavierkonzert Nr. 3, …).
    # When the user pasted "Composer: Werk", we instead require the work
    # tokens to be in the title and the canonical surname to match.
    work_q_parts = ([p for p in normalize_text(pasted_work_q).split() if p]
                    if pasted_work_q else q_parts)

    for w in WORKS:
        composer = w.get("composer", "")
        title = w.get("title", "")
        if not composer or not title:
            continue
        composer_norm = normalize_text(composer)
        title_norm = normalize_text(title)
        w_surname = composer_surname(composer)

        if pasted_composer:
            # Paste-mode: alias-match composer, then signature-match the
            # work half. Signature matching collapses "Klavierkonzert Nr. 2"
            # (Klassika-Spelling) and "2. Konzert für Klavier" (event
            # programme spelling) into the same {genre, number} pair.
            if target_surname != w_surname:
                continue
            work_sig = extract_work_signature(pasted_work_q)
            if not work_text_matches(title, work_sig):
                continue
            score = 70 + _famous_boost(composer)
        else:
            haystack = f"{composer_norm} {title_norm}"
            token_match = all(p in haystack for p in q_parts)
            alias_match = bool(target_surname) and target_surname == w_surname \
                          and all(p in title_norm for p in q_parts[1:] or q_parts)
            if not (token_match or alias_match):
                continue
            score = 50 + _famous_boost(composer)
            if len(q_parts) >= 2 and q_parts[0] in composer_norm:
                score += 20
        suggestions.append({
            "type": "work",
            "label": f"{composer} — {title}",
            "composer": composer,
            "work": title,
            "opus": w.get("opus", ""),
            "score": score,
        })

    # Dedupe by visible label, keep the highest-scoring instance.
    suggestions.sort(key=lambda x: -x["score"])
    seen: set = set()
    ranked: List[Dict] = []
    for s in suggestions:
        if s["label"] in seen:
            continue
        seen.add(s["label"])
        ranked.append(s)
        if len(ranked) >= limit:
            break

    # Drop the internal "score" before responding.
    for s in ranked:
        s.pop("score", None)
    return {"query": q, "suggestions": ranked}


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


@app.get("/api/filters")
def get_filter_options():
    """Aggregate the distinct filter values currently present in EVENTS,
    so the frontend can populate the city / venue / series dropdowns
    with only choices that actually have concerts behind them.

    Cities and venues come back sorted by event count (most-programmed
    first); series too. Date range is the min/max date in the data."""
    from collections import Counter
    from datetime import date as _date

    today = _date.today().isoformat()
    upcoming = [e for e in EVENTS if (e.get("date") or "0000-00-00") >= today]

    city_counts:   Counter = Counter()
    venue_counts:  Counter = Counter()
    series_counts: Counter = Counter()
    # Venue → which cities it lives in (so the frontend can show the right
    # venues when the user picks a city). Most venues are in exactly one city.
    venue_city: Dict[str, str] = {}
    dates: List[str] = []
    for e in upcoming:
        if e.get("city"):
            city_counts[e["city"]] += 1
        if e.get("venue"):
            venue_counts[e["venue"]] += 1
            if e.get("city"):
                venue_city[e["venue"]] = e["city"]
        if e.get("series"):
            series_counts[e["series"]] += 1
        if e.get("date"):
            dates.append(e["date"])

    return {
        "cities":  [{"name": c, "count": n} for c, n in city_counts.most_common()],
        "venues":  [{"name": v, "count": n, "city": venue_city.get(v, "")}
                    for v, n in venue_counts.most_common()],
        "series":  [{"name": s, "count": n} for s, n in series_counts.most_common()],
        "date_range": {
            "min": min(dates) if dates else today,
            "max": max(dates) if dates else today,
        },
        "total_upcoming": len(upcoming),
    }


def _parse_csv(value: Optional[str]) -> List[str]:
    """Turn 'a,b,c' query param into ['a','b','c']. Strips whitespace, drops empties."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


@app.get("/api/events")
def get_events(
    skip: int = 0,
    limit: int = 8,
    venue: Optional[str] = None,
    city: Optional[str] = None,
    series: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    has_stream: Optional[bool] = None,
    include_past: bool = False,
):
    """Paginated upcoming-concerts feed.

    Query parameters:
      - skip / limit:    pagination
      - venue, city, series:  comma-separated lists. Empty → no filter.
        Multi-value semantics: event matches if its value is in the list.
      - from_date / to_date:  inclusive YYYY-MM-DD bounds
      - has_stream=true:      only events with a streaming option
      - include_past=true:    keep past events (default: hide)
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    venues = _parse_csv(venue)
    cities = _parse_csv(city)
    series_filter = _parse_csv(series)

    pool = EVENTS
    if not include_past:
        pool = [e for e in pool if (e.get("date") or "0000-00-00") >= today]
    if venues:
        venue_set = set(venues)
        pool = [e for e in pool if e.get("venue") in venue_set]
    if cities:
        city_set = set(cities)
        pool = [e for e in pool if e.get("city") in city_set]
    if series_filter:
        series_set = set(series_filter)
        pool = [e for e in pool if e.get("series") in series_set]
    if from_date:
        pool = [e for e in pool if (e.get("date") or "0000-00-00") >= from_date]
    if to_date:
        pool = [e for e in pool if (e.get("date") or "9999-99-99") <= to_date]
    if has_stream:
        pool = [e for e in pool if e.get("has_stream")]

    total = len(pool)
    page = pool[skip : skip + limit]
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "filters": {
            "venues": venues, "cities": cities, "series": series_filter,
            "from_date": from_date, "to_date": to_date,
            "has_stream": bool(has_stream),
        },
        "has_more": (skip + limit) < total,
        "events": page,
    }
