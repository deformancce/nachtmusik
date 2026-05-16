"""Venue/hall/city resolution helpers — extracted from existing scrapers."""
from __future__ import annotations

# Cities that appear in guest-tour location strings. When we see one of these
# inside a location field, the event is a guest tour (not the home venue).
KNOWN_CITIES = (
    "Wien", "Hongkong", "Beijing", "Shanghai", "Taipei", "Tainan", "Wuxi",
    "Frankfurt", "München", "Hamburg", "Berlin", "Köln", "Dresden", "Salzburg",
    "London", "Paris", "New York", "Tokyo", "Tokio", "Zürich", "Amsterdam",
    "Stockholm", "Oslo", "Helsinki", "Prag", "Warschau", "Budapest",
    "Mailand", "Milano", "Rom", "Roma", "Madrid", "Barcelona",
)


def resolve_hall(
    location: str,
    hall_mappings: dict,
    default_venue: str,
    default_city: str,
) -> tuple[str, str, str]:
    """Resolve a raw location string to (venue, hall, city).

    Uses the per-venue hall_mappings first, then falls back to KNOWN_CITIES
    (guest-tour detection), then to defaults.

    Args:
        location:       Raw location text from the event teaser.
        hall_mappings:  Dict of hall_name → (venue, city) from VenueConfig.
        default_venue:  Default venue name when nothing else matches.
        default_city:   Default city when nothing else matches.

    Returns:
        (venue, hall, city) tuple.
    """
    if not location:
        return (default_venue, "", default_city)

    for hall_key, (venue, city) in hall_mappings.items():
        if hall_key in location:
            return (venue, hall_key, city)

    for city in KNOWN_CITIES:
        if city in location:
            return (location, "", city)  # guest tour: location IS the venue

    return (default_venue, location, default_city)
