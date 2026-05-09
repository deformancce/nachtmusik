"""
Scrape Gewandhaus Leipzig "Grosse Concerte" using Playwright.

The page lazy-loads events behind a "Weitere Veranstaltungen laden" button.
Playwright drives a real browser, scrolls, and clicks the button to expand
the list before extracting events.
"""
from typing import List
from pathlib import Path
import json
import time

from playwright.sync_api import sync_playwright

from .models import Event


def scrape_gewandhaus(max_clicks: int = 5) -> List[Event]:
    all_events: List[Event] = []
    seen_urls = set()

    print("Starting Playwright browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(60000)

        try:
            url = "https://www.gewandhausorchester.de/grosse-concerte/"
            print(f"Loading {url}...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print("Waiting for page to initialize...")
            time.sleep(5)

            print(f"Clicking 'Load More' up to {max_clicks} times...\n")
            clicks_successful = 0
            for i in range(max_clicks):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)
                    clicked = page.evaluate(
                        """
                        () => {
                            const button = document.querySelector('a[href*="javascript"]');
                            if (button && button.textContent.includes('Weitere')) {
                                button.click();
                                return true;
                            }
                            return false;
                        }
                        """
                    )
                    if clicked:
                        clicks_successful += 1
                        print(f"  Click {clicks_successful}/{max_clicks}")
                        time.sleep(3)
                    else:
                        print(f"  Button not found on attempt {i + 1}")
                        break
                except Exception as exc:
                    print(f"  Error on click {i + 1}: {str(exc)[:60]}")

            print(f"\nClicked {clicks_successful} times. Extracting events...\n")
            time.sleep(2)

            event_items = page.locator("div.event-list__item").all()
            print(f"Found {len(event_items)} event blocks")

            for item in event_items:
                try:
                    date = ""
                    time_str = ""
                    hall = "Gewandhaus Leipzig"

                    try:
                        date_info = item.locator("div.event-teaser__date-info").first
                        p_tags = date_info.locator("p").all()
                        if len(p_tags) >= 2:
                            date = p_tags[1].inner_text().strip()
                        time_el = date_info.locator("span.event-teaser__time")
                        if time_el.count() > 0:
                            time_str = time_el.first.inner_text().strip()
                        if len(p_tags) >= 3:
                            hall_text = p_tags[2].inner_text().strip()
                            if time_str:
                                hall_text = hall_text.replace(time_str, "").strip()
                            if hall_text:
                                hall = hall_text
                    except Exception:
                        pass

                    title = ""
                    try:
                        short_desc = item.locator(
                            "div.event-teaser__short-description"
                        ).first
                        title_h2 = short_desc.locator("h2")
                        if title_h2.count() > 0:
                            title = title_h2.first.inner_text().strip()
                        else:
                            title_p = short_desc.locator("p.text-highlight")
                            if title_p.count() > 0:
                                title = title_p.first.inner_text().strip()
                    except Exception:
                        pass

                    if not title:
                        continue

                    detail_url = url
                    try:
                        link_el = item.locator(
                            "a.event-teaser__short-description-link"
                        ).first
                        href = link_el.get_attribute("href")
                        if href:
                            detail_url = (
                                f"https://www.gewandhausorchester.de{href}"
                                if href.startswith("/")
                                else href
                            )
                    except Exception:
                        pass

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    composers: List[str] = []
                    try:
                        composer_ps = item.locator(
                            "div.event-teaser__composer-info p"
                        ).all()
                        for el in composer_ps:
                            text = el.inner_text().strip()
                            if text.startswith("Werke von"):
                                composers_text = text.replace("Werke von", "").strip()
                                composers = [c.strip() for c in composers_text.split(",")]
                                break
                    except Exception:
                        pass

                    if composers:
                        composers_str = ", ".join(composers[:3])
                        if len(composers) > 3:
                            composers_str += " u.a."
                        enhanced_title = f"{title} — {composers_str}"
                    else:
                        enhanced_title = title

                    all_events.append(
                        Event(
                            source="Gewandhaus Leipzig - Grosse Concerte",
                            title=enhanced_title,
                            date=date,
                            time=time_str,
                            location=hall,
                            url=detail_url,
                            composer=", ".join(composers) if composers else None,
                        )
                    )
                except Exception:
                    continue
        except Exception as exc:
            print(f"Error: {exc}")
        finally:
            browser.close()

    print(f"\nScraped {len(all_events)} unique events")
    return all_events


def save_events_to_json(
    events: List[Event],
    filename: str = "gewandhaus_events.json",
) -> Path:
    output_path = Path(__file__).parent.parent / filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total": len(events),
                "source": "gewandhausorchester.de",
                "scraped_date": time.strftime("%Y-%m-%d"),
                "events": [e.to_dict() for e in events],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Saved to: {output_path}")
    return output_path


if __name__ == "__main__":
    events = scrape_gewandhaus()
    if events:
        save_events_to_json(events)
        print("\nFirst 3 events:")
        for e in events[:3]:
            print(f"  {e.date} {e.time}: {e.title}")
