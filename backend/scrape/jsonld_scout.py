"""
JSON-LD scout: cheap pre-pass before Firecrawl.

Many German cultural sites ship Schema.org Event objects in
<script type="application/ld+json"> blocks for SEO. When present, they
are 100% deterministic, complete, and cost 0 Firecrawl credits.

Usage:
    events = scout("https://www.alteoper.de/de/programm")
    if events:   # list of dicts with at least title + date
        ...      # skip Firecrawl listing call
    else:        # None or empty
        ...      # fall back to Firecrawl
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


_HEADERS = {
    # Chrome on macOS — many German cultural sites block obvious bot UAs.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}


def _walk_for_events(obj, out: list[dict]) -> None:
    """Recursively collect Schema.org Event-typed nodes from a JSON-LD blob."""
    if isinstance(obj, list):
        for item in obj:
            _walk_for_events(item, out)
        return
    if not isinstance(obj, dict):
        return
    t = obj.get("@type")
    types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
    is_event = any("Event" in (typ or "") for typ in types if isinstance(typ, str))
    if is_event and obj.get("name"):
        out.append(obj)
    for key in ("@graph", "itemListElement", "subEvent", "events", "item"):
        if key in obj:
            _walk_for_events(obj[key], out)


def _iso_date(s) -> Optional[str]:
    if not isinstance(s, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _iso_time(s) -> Optional[str]:
    if not isinstance(s, str):
        return None
    m = re.search(r"T(\d{2}:\d{2})", s)
    return m.group(1) if m else None


def _hall_from_location(loc) -> Optional[str]:
    if isinstance(loc, dict):
        return loc.get("name")
    if isinstance(loc, list) and loc and isinstance(loc[0], dict):
        return loc[0].get("name")
    return None


def _performers(perf) -> list[str]:
    if not perf:
        return []
    if isinstance(perf, dict):
        perf = [perf]
    if not isinstance(perf, list):
        return []
    out = []
    for p in perf:
        if isinstance(p, dict) and p.get("name"):
            out.append(p["name"])
        elif isinstance(p, str):
            out.append(p)
    return out


def _price_from_offers(offers) -> Optional[str]:
    if not offers:
        return None
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None
    prices: list[float] = []
    currency = "EUR"
    for o in offers:
        if not isinstance(o, dict):
            continue
        if o.get("priceCurrency"):
            currency = o["priceCurrency"]
        p = o.get("price")
        if p is None:
            continue
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    sym = "€" if currency == "EUR" else currency + " "
    def fmt(x: float) -> str:
        return f"{sym}{int(x)}" if x.is_integer() else f"{sym}{x:.2f}"
    if len(prices) == 1 or min(prices) == max(prices):
        return fmt(prices[0])
    return f"ab {fmt(min(prices))}"


def _ld_to_event(ld: dict, base_url: str) -> Optional[dict]:
    name = ld.get("name") or ld.get("headline")
    start = ld.get("startDate") or ld.get("startTime")
    date = _iso_date(start)
    if not name or not date:
        return None
    detail = ld.get("url") or ld.get("@id")
    if isinstance(detail, str) and detail and not detail.startswith("http"):
        detail = urljoin(base_url, detail)
    if not isinstance(detail, str):
        detail = None
    return {
        "title": name.strip(),
        "date": date,
        "time": _iso_time(start),
        "venue_hall": _hall_from_location(ld.get("location")),
        "program": [],  # JSON-LD rarely has the program; enrichment fills it
        "performers": _performers(ld.get("performer")),
        "conductor": None,
        "price": _price_from_offers(ld.get("offers")),
        "detail_url": detail,
    }


def scout(url: str, timeout: int = 10, verbose: bool = True) -> Optional[list[dict]]:
    """Try to extract Event objects from a page's JSON-LD blocks.

    Returns:
      - list of event dicts (possibly empty after dedup) if JSON-LD with
        Event objects was found
      - None if the page has no JSON-LD Events (caller falls back to Firecrawl)
    """
    def _log(msg: str) -> None:
        if verbose:
            print(f"    [jsonld] {msg}", flush=True)

    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
    except Exception as exc:
        _log(f"fetch error: {type(exc).__name__}: {exc}")
        return None
    if r.status_code != 200:
        _log(f"HTTP {r.status_code} — skipping")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    if not scripts:
        _log("no <script type='application/ld+json'> blocks")
        return None
    _log(f"found {len(scripts)} JSON-LD block(s)")

    raw_events: list[dict] = []
    for s in scripts:
        text = s.string or s.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        _walk_for_events(data, raw_events)

    if not raw_events:
        _log("0 Event-typed nodes inside the JSON-LD blocks")
        return None

    events: list[dict] = []
    seen: set[tuple] = set()
    for raw in raw_events:
        ev = _ld_to_event(raw, url)
        if not ev:
            continue
        key = (ev.get("detail_url"), ev.get("date"), ev.get("title"))
        if key in seen:
            continue
        seen.add(key)
        events.append(ev)
    _log(f"parsed {len(events)} unique events from JSON-LD")
    return events
