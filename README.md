# op.us

Find classical music live.

op.us is a search interface for classical concerts in Germany. It combines venue
scrapers, structured repertoire data, and a fast search frontend so listeners can
look up composers, works, and upcoming performances in one place.

Live site: [nachtmusik.netlify.app](https://nachtmusik.netlify.app)

## Project Structure

```
op.us/
├── backend/
│   ├── app.py                          # FastAPI server (smart search)
│   ├── requirements.txt
│   ├── venues_germany.py               # 100 German concert venues
│   └── scrape/
│       ├── models.py                   # Event data model
│       ├── composers_klassika.py       # Composers from klassika.info (A-Z)
│       ├── works_klassika.py           # Works per composer
│       ├── gewandhaus_full.py          # Gewandhaus Leipzig events (Playwright)
│       └── smart_venue_scraper.py      # Universal scraper using Claude API
└── frontend/
    └── index.html                      # Search UI (purple/green op.us theme)
```

## Setup

### 1. Backend dependencies

```bash
cd backend
pip3 install -r requirements.txt
playwright install chromium   # only needed for gewandhaus_full.py
```

### 2. Scrape the database (one-time, ~10–30 min)

```bash
cd backend
python3 scrape/composers_klassika.py   # → composers_klassika.json (~6,500 composers)
python3 scrape/works_klassika.py       # → works_klassika.json (works per composer)
python3 scrape/gewandhaus_full.py      # → gewandhaus_events.json (optional)
```

The generated JSON files are gitignored — they're regenerated locally.

### 3. Run the backend

```bash
cd backend
python3 -m uvicorn app:app --reload --port 8000
```

You should see:
```
✓ Loaded 6574 composers
✓ Loaded XXXX works
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 4. Run the frontend

In a new terminal:

```bash
cd frontend
python3 -m http.server 3000
```

Open http://localhost:3000 in your browser.

## API Endpoints

- `GET /` — info & stats
- `GET /api/search?q=brahms 1` — smart search (composers + works)
- `GET /api/composer/{name}/works` — all works for a composer
- `GET /api/work/performances?work_title=...&composer=...` — concerts performing a work

## Smart Venue Scraper (optional, uses Claude API)

The universal scraper uses the Anthropic API to extract events from any venue website
without writing custom selectors per site.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python3 scrape/smart_venue_scraper.py
```

This iterates through `venues_germany.VENUES_GERMANY` and writes `all_venues_events.json`.
