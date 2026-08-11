# Voyage — AI Travel Agent for Trip.com

Local desktop travel concierge inspired by [Trip.com Trip.Planner](https://hk.trip.com/webapp/tripmap/tripplanner?source=t_online_homepage&locale=en-HK&curr=HKD). It uses **Ollama** for planning and a **Playwright** Chromium session on **[hk.trip.com](https://hk.trip.com)** for live flights, hotels, and optional car rentals.

## What it does

1. **Plan your trip** — destination, dates/nights, budget, travel style, and optional **Rent a car**
2. **Propose a route** — single-city or multi-city regional stays (e.g. California → SF / LA / SD) with open-jaw airports when useful
3. **Search live on Trip.com** — flights, hotels (with real detail links, names, scores, photos), and car hire when enabled
4. **Build an itinerary board** — flight card, hotel card(s), optional car card, and day-by-day sightseeing
5. **Refine in chat** — tweak the plan without leaving the desktop UI

Booking links open real `hk.trip.com` pages in your browser. The model is instructed **not** to invent Trip.com URLs.

## Prerequisites

1. [Ollama](https://ollama.com/) installed and able to run locally  
2. A tool-capable chat model (default: `qwen2.5:3b` — lighter/faster locally)  
3. Python **3.10+**

```bash
ollama serve
ollama pull qwen2.5:3b
```

## Setup

```bash
cd "d:\An AI Agent for travel planning"
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Edit `.env` if needed:

| Variable | Default | Notes |
|----------|---------|--------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Override with `-m` (e.g. `qwen2.5:7b` for quality) |
| `TRIP_BASE_URL` | `https://hk.trip.com` | Market site |
| `TRIP_LOCALE` | `en_hk` | Locale query param |
| `TRIP_CURRENCY` | `HKD` | Prices in HKD |
| `HEADLESS` | `true` | Hide Chromium while scraping |
| `FAST_MODE` | `true` | Skip extra LLM ranking / attraction-arrange calls |
| `LLM_RANK_CANDIDATES` | `false` | Set `true` (+ `FAST_MODE=false`) to LLM-rank flights/hotels/cars |
| `MAX_TOOL_ROUNDS` | `8` | Cap agent↔tool loops |
| `OLLAMA_NUM_CTX` | `4096` | Context window (lower = faster) |
| `OLLAMA_NUM_PREDICT` | `768` | Max new tokens per reply |

## Run

```bash
python -m travel_agent
```

Opens the **desktop GUI** (default). Trip.com scraping is **headless** unless you pass:

```bash
python -m travel_agent --show-browser
```

### Windows EXE

Build a double-clickable app (no Python needed to *run* after build):

```bat
build_exe.bat
```

Then open:

`dist\VoyageRelease\Voyage.exe`

(Close any running Voyage window before rebuilding — Windows locks the old folder.)

Still required on the PC:

1. [Ollama](https://ollama.com/) + `ollama pull qwen2.5:3b`
2. Chromium for Playwright (once): `python -m playwright install chromium`  
   (or install Google Chrome / Microsoft Edge — Voyage falls back to them)

Copy or edit `dist\VoyageRelease\.env` next to the EXE for model / speed settings. Rebuild with the same `build_exe.bat` after code changes.

Use another Ollama model:

```bash
python -m travel_agent -m qwen2.5:7b
```

Terminal chat instead of the window:

```bash
python -m travel_agent --cli
python -m travel_agent -q "Plan 5 nights in Tokyo from Hong Kong"
```

Header button **Open Trip.Planner** launches the official web product for comparison.

## Typical agent flow

1. **`propose_trip_route`** — choose fly-into / fly-out airports and hotel cities for the trip length  
2. **`plan_trip`** — live Trip.com search for flights + hotels (+ cars if **Rent a car** is checked)  
3. GUI renders structured cards from scraped fields and trusted booking URLs  

Supporting tools (used as needed): `search_flights`, `search_hotels`, `search_cars`, `search_trains`, `search_transfers`, attraction scraping / day arrangement.

### Hotels

- Hotels: list results → pick a property (cheapest/first in FAST_MODE, or LLM when ranking is on) → booking URL is `/hotels/detail/?hotelId=…`
- Name / score / photo / price come from the matching list card and/or HTTP detail HTML (Playwright often cannot open detail pages due to sign-in redirects)
- Multi-city trips search **each stay city** separately so cards keep their own hotel link, name, and image

### Car rental (optional)

- Only when **Rent a car** is enabled (otherwise car search is skipped)
- Scrapes Trip.com list cards (`.vehicle-item-fuse`) for name, specs, vendor, price, and `img.vehicle-item-fuse__image`
- **View deal** opens the scraped `/carrentals/detail` link

## Project layout

```
travel_agent/
  gui.py                 # Desktop wizard + itinerary board (flights / hotels / car / days)
  agent.py               # Ollama tool-calling loop + booking_links merge
  browser_tools.py       # Playwright Trip.com tools (flights, hotels, cars, plan_trip)
  planner_query.py       # Prompt builder from GUI context
  itinerary_parse.py     # Parse LLM text into structured offers / day blocks
  trip_urls.py           # Normalize / trust Trip.com flight & hotel & car URLs
  regions.py             # Multi-city regional routes + night alignment
  llm_select.py          # LLM ranking of scraped flight / hotel / car candidates
  destination_guides.py  # Day-idea fallbacks when scrape is thin
  attraction_images.py   # Attraction / fallback image helpers
  places.py              # City ↔ airport / hotel city helpers
  pricing.py             # HKD price parse + comparisons
  airline_names.py       # Airline code / logo helpers
  ollama_lifecycle.py    # Start / ensure Ollama + model availability
  cli.py                 # Entry: GUI by default, --cli / -q for terminal
  config.py              # Settings from .env

tests/                   # Trip example suite (logic + optional live scrape)
scripts/                 # Ad-hoc debug helpers for scraping / cards
voyage.py                # Frozen EXE entry point
voyage.spec              # PyInstaller build definition
build_exe.bat            # One-click Windows EXE build
```

## Tests

```bash
# Fast logic checks (no browser)
python -m tests.run_trip_examples

# Live Trip.com scrape for the example set (slow)
python -m tests.run_trip_examples_live
python -m tests.run_trip_examples_live --resume
python -m tests.run_trip_examples_live --ids 1,27,50
python -m tests.run_trip_examples_live --mode scrape
```

Results go under `tests/live_results/` (`progress.jsonl`, transcripts, `summary.json`).

## Notes

- Live prices and booking links come from Trip.com scrape / tool output — not model hallucination.
- Closing the app does **not** stop Ollama (so other tools can keep using it).
- No embedded map; use day cards plus Open Trip.Planner / booking links.
- Default model: `qwen2.5:3b` (`OLLAMA_MODEL` or `-m` to override). With `FAST_MODE=true`, Voyage skips extra Ollama ranking calls and uses heuristics for flights/hotels/cars/day sights.

## License / attribution

Trip.com and Trip.Planner are trademarks of their respective owners. This project is an independent local client that automates a browser session against the public Hong Kong site for personal planning use.
