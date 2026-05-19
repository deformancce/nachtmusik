"""
Heuristic event classifier: classical vs non-classical.

These venues are predominantly classical-music halls but also host pop, musicals,
comedy and tribute shows (cross-promotion). We tag each event with `is_classical`
based on title, program and performer fields. Frontend can filter / sort by this.

Strategy:
1. Strong positive signals (composer name, ensemble name, classical genre) → True.
2. Strong negative signals (pop artist name, musical/tribute keyword) → False.
3. Otherwise default to True (these are classical venues; absence of negative
   signals is more likely a sparse/unenriched classical event than a hidden pop one).

Jazz is grouped with classical here — these halls programme jazz on their main
series, and users searching for serious music expect both.
"""
from __future__ import annotations

import re


_CLASSICAL_COMPOSERS = (
    "bach", "beethoven", "brahms", "mozart", "schubert", "schumann",
    "mahler", "bruckner", "haydn", "handel", "händel", "wagner",
    "verdi", "chopin", "liszt", "mendelssohn", "tchaikovsky",
    "tschaikowsky", "tschaikowski", "dvořák", "dvorak", "sibelius",
    "ravel", "debussy", "rachmaninoff", "rachmaninow", "rachmaninov",
    "shostakovich", "schostakowitsch", "stravinsky", "strawinsky",
    "prokofiev", "prokofjew", "bartók", "bartok", "strauss",
    "fauré", "faure", "berlioz", "puccini", "rossini", "monteverdi",
    "rameau", "vivaldi", "scarlatti", "telemann", "saint-saëns",
    "saint-saens", "elgar", "britten", "rihm", "ligeti", "messiaen",
    "boulez", "stockhausen", "hindemith", "korngold", "schönberg",
    "schoenberg", "berg", "webern", "weill", "orff", "henze", "ives",
    "copland", "barber", "gershwin", "bernstein", "glass",
    "smetana", "janáček", "janacek", "grieg", "nielsen",
    "respighi", "mascagni", "leoncavallo", "donizetti", "bellini",
)

_CLASSICAL_GENRE_KEYWORDS = (
    "symphonie", "sinfonie", "symphony",
    "konzert für ", "klavierabend", "violinabend", "violinrecital",
    "kammermusik", "streichquartett", "klavierquartett",
    "klaviertrio", "klavierquintett", "klavierkonzert",
    "violinkonzert", "cellokonzert", "hornkonzert",
    "lieder", " lied ", "liederabend",
    "oratorium", "requiem", "messe ", "kantate", "passion",
    "rezital", "recital", "matinee", "matinée",
    "philharmonisch", "sinfonisch", "barock",
    "sonate", "sonata", "konzertsaison",
    "orgelmatinee", "orgelkonzert",
    "lunchkonzert",
    "kammer­konzert", "kammerkonzert",
    "meisterkonzert", "abonnement",
    "chormusik", "kirchenmusik", "geistliche",
)

_CLASSICAL_PERFORMER_KEYWORDS = (
    "philharmoniker", "philharmonia",
    "sinfonieorchester", "symphonieorchester", "symphony orchestra",
    "staatsorchester", "staatskapelle",
    "kammerphilhar", "kammerorchester",
    "konzerthausorchester", "rundfunk-sinfonie",
    "rundfunkorchester", "rundfunkchor",
    "concertgebouw", "wdr sinfonie", "br sinfonie",
    "ndr elbphil", "ndr sinfonie", "swr sinfonie",
    "hr-sinfonie", "hr sinfonie", "rsb ", "dso ", "rsb,",
    "kammerchor", "knabenchor", "thomanerchor",
    "ensemble modern", "klangforum",
    "academy of",  # e.g. Academy of St Martin in the Fields
    "il giardino", "concentus musicus", "concerto köln",
    "akademie für alte musik",
    "freiburger barockorchester",
    "gewandhausorchester",
    "lang lang", "hayato sumino", "lukas sternath",
    "quartett", "quartet",
)

_JAZZ_KEYWORDS = (
    "jazz", "bigband", "big band", "miles davis", "john coltrane",
)

_OPERA_KEYWORDS = (
    "oper:", " oper ", "opera", "opernabend",
)

# --- negative signals -------------------------------------------------------

_NON_CLASSICAL_ARTISTS = (
    # Pop / rock / singer-songwriter
    "anastacia", "tim bendzko", "rea garvey", "tina dico",
    "olegg vynnyk", "mabel matiz", "louis tomlinson",
    "steve hackett", "element of crime", "rea garvey",
    "ibrahim selim", "bee gees", "naturally 7", "dionne warwick",
    "die prinzen", "max raabe",  # actually max raabe is borderline cabaret
    "the constellation choir",  # not classical
    "olli schulz", "nino d'angelo", "nino dangelo", "olaf der flipper",
    "andreas gabalier", "rammstein", "pink floyd", "manfred mann",
    "fischer-z", "the sweet", "joe jackson", "mike oldfield",
    # Comedy / cabaret / spoken word
    "marc-uwe kling", "umbilical brothers", "hagen rether",
    "die deutschen podcast", "reiner calmund",
    "alexander stevens", "jacqueline belle",
    "kabarett", "kabaret", "comedy", "stand-up", "standup",
    "peter wohlleben", "ferdinand von schirach", "sebastian fitzek",
    "tahsim durgun", "parshad", "lisa eckhart", "simon stäblein",
    "nick martin", "swr1 pop", "pop & poesie",
    # Musicals / tribute / show
    "mamma mia", "pretty woman - das musical",
    "by maincourse",  # tribute act
    "cornamusa",  # folk dance show
    "kängur",  # Marc-Uwe Kling kangaroo books
    "jobe messe", "spezialmesse", "karrieretag",
    "circus", "flamenco dance",
)

_NON_CLASSICAL_TITLE_RE = re.compile(
    r"\b(?:"
    r"tribute|"
    r"das musical|"
    r"the musical|"
    r"musical[\s-]?show|"
    r"live\s?20\d{2}|"
    r"best of genesis|"
    r"-?\s?tour[\s-]?20\d{2}|"
    r"podcast(?:\s|$)|"
    r"comedy[\s-]?show|"
    r"live[\s-]?hörspiel|"
    r"live[\s-]?hoerspiel|"
    r"lesung|vortrag|jobmesse|karrieretag|"
    r"spezialmesse|pop\s*&\s*poesie|"
    r"german open championships|"
    r"final fantasy|"
    r"christmas calling|"
    r"true crime|"
    r"freunde|fußball|"
    r"weihnachtsshow"
    r")\b",
    re.IGNORECASE,
)


def _haystack(event: dict) -> str:
    parts = [
        event.get("title") or "",
        " ".join(event.get("program") or []),
        " ".join(event.get("performers") or []),
        event.get("conductor") or "",
    ]
    return " ".join(parts).lower()


def has_classical_signal(event: dict) -> bool:
    """Return True only when the event contains an explicit positive signal."""
    text = _haystack(event)
    # Avoid treating job fairs as liturgical "Messe" concerts.
    if any(kw in text for kw in ("jobe messe", "spezialmesse", "karrieretag")):
        return False
    return any((
        any(c in text for c in _CLASSICAL_COMPOSERS),
        any(kw in text for kw in _CLASSICAL_PERFORMER_KEYWORDS),
        any(kw in text for kw in _CLASSICAL_GENRE_KEYWORDS),
        any(kw in text for kw in _OPERA_KEYWORDS),
        any(kw in text for kw in _JAZZ_KEYWORDS),
    ))


def has_non_classical_signal(event: dict) -> bool:
    """Return True for strong pop, comedy, musical, show, or event-fair signals."""
    text = _haystack(event)
    return any(kw in text for kw in _NON_CLASSICAL_ARTISTS) or bool(_NON_CLASSICAL_TITLE_RE.search(text))


def is_classical_event(event: dict) -> bool:
    """Return True if the event is classical music (or jazz, opera, lieder).
    False for pop, rock, musicals, tribute shows, comedy, cabaret.
    Defaults to True when no strong signal — these are classical venues."""
    # 1. Strong positive — composer or classical ensemble named
    if has_classical_signal(event):
        return True

    # 2. Strong negative — known pop/musical/comedy markers
    if has_non_classical_signal(event):
        return False

    # 3. Default: classical (venues are classical halls, ambiguous = probably classical)
    return True
