"""Shared validators — extracted from gewandhaus_scraper.py and berliner_philharmonie_scraper.py."""
from __future__ import annotations
import re
import unicodedata


def entry_has_mojibake(text: str) -> bool:
    """Detect encoding corruption (Mojibake) — typical German characters that survived
    double-encoding look like 'Ã¼' (ü) or 'Ã¶' (ö). If a text contains these
    sequences, the encoding fix didn't work and the entry should be dropped."""
    if not text:
        return False
    mojibake_patterns = [
        "Ã¤", "Ã¶", "Ã¼", "Ã„", "Ã–", "Ãœ", "Ã\x9f",  # German umlauts double-encoded
        "Ã©", "Ã¨", "Ã\xa0", "Ã®", "Ã\xaf",              # French accents double-encoded
        "\xe2\x80\x93", "\xe2\x80\x98", "\xe2\x80\x99",   # Smart quotes (raw bytes as str)
    ]
    return any(p in text for p in mojibake_patterns)


def is_bad_title(title: str) -> bool:
    """Reject titles that are clearly not concert titles — navigation fragments,
    generic labels, empty strings, or single-character strings."""
    if not title or len(title.strip()) < 3:
        return True
    bad_patterns = [
        r"^(Konzerte?|Events?|Programm|Spielplan|Veranstaltungen?|Tickets?)$",
        r"^(Mehr laden|Load more|Weitere|Zurück|Next|Previous)$",
        r"^(Navigation|Menu|Footer|Header|Sidebar)$",
    ]
    title_stripped = title.strip()
    for pat in bad_patterns:
        if re.match(pat, title_stripped, re.IGNORECASE):
            return True
    return False


def title_matches_program(title: str, program: list) -> bool:
    """Sanity-check for opera-style ALL-CAPS titles ('CARMEN', 'REGINA', ...).

    If the title is all-uppercase and its key tokens don't appear in the first
    program entry, it's likely a cross-promotion bleed from a cached earlier run.
    Returns True if consistent, or if the title isn't ALL-CAPS (no policing needed).
    """
    if not title or not program or not isinstance(program[0], str):
        return True
    letters = [c for c in title if c.isalpha()]
    if not letters or not all(c.isupper() for c in letters):
        return True
    title_tokens = [t.lower() for t in title.split()
                    if len(t) > 2 and t.lower() not in ("der", "die", "das", "the")]
    if not title_tokens:
        return True
    prog_lower = program[0].lower()
    return any(t in prog_lower for t in title_tokens)


def fix_encoding(text: str) -> str:
    """Attempt to fix double-encoded UTF-8 (common on German sites with incorrect
    Content-Type headers). Falls back to the original if decoding fails."""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def normalize_nfc(text: str) -> str:
    """Normalize unicode to NFC — ensures consistent representation of umlauts."""
    return unicodedata.normalize("NFC", text or "")
