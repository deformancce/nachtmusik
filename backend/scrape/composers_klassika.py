"""
Scrape all composers from klassika.info (A-Z).

Each letter has its own index page: /Komponisten/lindex_{LETTER}.html
Composer entries link to /Komponisten/{Folder}/index.html with text like
"Aaltoila, Heikki (1905-1992)" or "Aagaard-Nilsen, Torstein (geb. 1964)".
"""
from typing import List, Set
from collections import Counter
from pathlib import Path
import json
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "op.us prototype scraper (https://github.com/deformancce/op.us)"
}
BASE_URL = "https://www.klassika.info"


def scrape_composers_from_klassika() -> List[dict]:
    all_composers: List[dict] = []
    seen_names: Set[str] = set()
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    print("Klassika.info Composer Scraper (A-Z)")
    print("=" * 60)

    for letter in letters:
        url = f"{BASE_URL}/Komponisten/lindex_{letter}.html"
        try:
            print(f"\nLetter: {letter}")
            print("  Fetching... ", end="", flush=True)

            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            composer_links = soup.select('a[href*="/Komponisten/"]')
            composer_links = [
                link for link in composer_links
                if "index_" not in link.get("href", "")
                and "lindex_" not in link.get("href", "")
                and "/index.html" in link.get("href", "")
            ]

            page_composers = 0
            for link in composer_links:
                text = link.get_text(strip=True)
                href = link.get("href", "")
                if not text or len(text) < 3 or text in seen_names:
                    continue
                seen_names.add(text)
                page_composers += 1

                name = text
                years = ""
                birth_year = None
                death_year = None

                if "(" in text:
                    name = text[: text.index("(")].strip()
                    years = text[text.index("(") + 1 : text.rindex(")")].strip()
                    if "-" in years and "geb" not in years:
                        parts = years.split("-")
                        if len(parts) == 2:
                            try:
                                birth_year = int(parts[0].strip())
                                death_year = int(parts[1].strip())
                            except ValueError:
                                pass
                    elif "geb" in years:
                        try:
                            birth_year = int(years.replace("geb.", "").strip())
                        except ValueError:
                            pass

                full_url = f"{BASE_URL}{href}" if href.startswith("/") else href
                all_composers.append({
                    "name": name,
                    "name_full": text,
                    "years": years,
                    "birth_year": birth_year,
                    "death_year": death_year,
                    "letter": letter,
                    "url": full_url,
                    "source": "klassika.info",
                })

            print(f"{page_composers} composers")
            time.sleep(0.5)
        except Exception as exc:
            print(f"error: {str(exc)[:60]}")
            continue

    print("\n" + "=" * 60)
    print(f"Total: {len(all_composers)} composers")
    return all_composers


def save_composers_to_json(
    composers: List[dict],
    filename: str = "composers_klassika.json",
) -> Path:
    output_path = Path(__file__).parent.parent / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total": len(composers),
                "source": "klassika.info",
                "scraped_date": time.strftime("%Y-%m-%d"),
                "composers": composers,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Saved to: {output_path}")

    centuries = []
    for c in composers:
        if c.get("birth_year"):
            century = (c["birth_year"] // 100) + 1
            centuries.append(century)
    if centuries:
        century_counts = Counter(centuries)
        print("\nBy century:")
        for century in sorted(century_counts.keys()):
            print(f"  {century}. Jh.: {century_counts[century]}")

    return output_path


if __name__ == "__main__":
    composers = scrape_composers_from_klassika()
    if composers:
        save_composers_to_json(composers)
