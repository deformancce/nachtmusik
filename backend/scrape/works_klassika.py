"""
Scrape works for each composer from klassika.info.

For each composer, fetches /Komponisten/{Folder}/wv_gattung.html — the work
catalog grouped by genre. Works are listed in HTML tables with columns
[Title, Opus/Catalog Number, Info].
"""
from typing import List, Dict
from pathlib import Path
import json
import re
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "op.us prototype scraper (https://github.com/deformancce/op.us)"
}


def get_composer_folder(composer_url: str) -> str | None:
    match = re.search(r"/Komponisten/([^/]+)/", composer_url)
    return match.group(1) if match else None


def scrape_works_for_composer(composer_url: str, composer_name: str) -> List[Dict]:
    works: List[Dict] = []
    folder = get_composer_folder(composer_url)
    if not folder:
        return works

    wv_url = f"https://www.klassika.info/Komponisten/{folder}/wv_gattung.html"
    try:
        response = requests.get(wv_url, headers=HEADERS, timeout=20)
        if response.status_code == 404:
            return works
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        current_genre = ""
        for table in soup.find_all("table"):
            heading = table.find_previous(["h2", "h3"])
            if heading:
                current_genre = heading.get_text(strip=True)

            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                title = cells[0].get_text(strip=True)
                opus = cells[1].get_text(strip=True)
                if not title or len(title) < 2:
                    continue
                # Skip header rows
                if "KV" in title and len(title) < 10:
                    continue
                works.append({
                    "title": title,
                    "opus": opus,
                    "genre": current_genre,
                    "composer": composer_name,
                    "source": "klassika.info",
                })
    except Exception:
        pass
    return works


def scrape_all_works(limit: int | None = None) -> List[Dict]:
    composers_file = Path(__file__).parent.parent / "composers_klassika.json"
    if not composers_file.exists():
        print("composers_klassika.json not found — run composers_klassika.py first")
        return []

    with open(composers_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    composers = data["composers"]
    if limit:
        composers = composers[:limit]
        print(f"TEST MODE: first {limit} composers\n")

    print(f"Scraping works for {len(composers)} composers")
    print("=" * 60)

    all_works: List[Dict] = []
    with_works = 0
    without_works = 0

    for i, composer in enumerate(composers, 1):
        name = composer["name"]
        url = composer["url"]
        display_name = (name[:32] + "...") if len(name) > 35 else name
        print(f"{i:>4}/{len(composers)} {display_name:<35} ", end="", flush=True)

        works = scrape_works_for_composer(url, name)
        if works:
            print(f"{len(works):>4} works")
            all_works.extend(works)
            with_works += 1
        else:
            print("     -")
            without_works += 1

        time.sleep(0.2)
        if i % 100 == 0:
            avg = len(all_works) / with_works if with_works else 0
            print(f"\n  Progress: {i}/{len(composers)} | {len(all_works)} works | {avg:.1f}/composer\n")

    print("\n" + "=" * 60)
    print(f"Composers with works: {with_works}")
    print(f"Composers without:    {without_works}")
    print(f"Total works:          {len(all_works)}")
    return all_works


def save_works_to_json(works: List[Dict], filename: str = "works_klassika.json") -> Path:
    output_path = Path(__file__).parent.parent / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total": len(works),
                "source": "klassika.info",
                "scraped_date": time.strftime("%Y-%m-%d"),
                "works": works,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Saved to: {output_path}")
    return output_path


if __name__ == "__main__":
    print("Test run with first 10 composers...\n")
    works = scrape_all_works(limit=10)
    if works:
        print(f"\nFound {len(works)} works from 10 composers")
        for work in works[:10]:
            opus = f" {work['opus']}" if work["opus"] else ""
            genre = f" [{work['genre']}]" if work["genre"] else ""
            print(f"  - {work['composer']}: {work['title']}{opus}{genre}")
        answer = input("\nContinue with ALL composers? (y/n): ")
        if answer.lower() == "y":
            all_works = scrape_all_works()
            save_works_to_json(all_works)
        else:
            print("Run later: python3 scrape/works_klassika.py")
    else:
        print("No works found")
