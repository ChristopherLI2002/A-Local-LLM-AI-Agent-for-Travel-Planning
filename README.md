# Voyage — AI Travel Agent for Trip.com

Local desktop travel concierge inspired by [Trip.com Trip.Planner](https://hk.trip.com/webapp/tripmap/tripplanner?source=t_online_homepage&locale=en-HK&curr=HKD). It uses **Ollama** for planning and a **Playwright** Chromium session on **[hk.trip.com](https://hk.trip.com)** for live flights, hotels, attractions, and optional car rentals.

## What it does

1. **Plan your trip** — destination, dates/nights, budget, travel style, and optional **Rent a car**
2. **Propose a route** — single-city or multi-city regional stays (e.g. California → SF / LA / SD) with open-jaw airports when useful
3. **Search live on Trip.com** — flights, hotels (detail links, names, scores, photos), attractions, and car hire when enabled
4. **Build an itinerary board** — flight card, hotel card(s), optional car card, and day-by-day sightseeing with timed stops
5. **Refine in chat** — tweak the plan without leaving the desktop UI

Booking links open real `hk.trip.com` pages in your browser. The model is instructed **not** to invent Trip.com URLs.

## Prerequisites

1. [Ollama](https://ollama.com/) installed and able to run locally  
2. A tool-capable chat model (default: `qwen2.5:3b` — lighter/faster locally)  
3. Python **3.10+** (only needed for source runs / building the EXE)  
4. Playwright Chromium (or Google Chrome / Microsoft Edge as fallback)

```bash
ollama serve
ollama pull qwen2.5:3b
```

## Setup (from source)

```bash
cd "d:\An AI Agent for travel planning"
python -m pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
```

Edit `.env` if needed:

| Variable | Default | Notes |
|----------|---------|--------|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama API (use IPv4 loopback, not `localhost`, on Windows) |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Override with `-m` (e.g. `qwen2.5:7b` for quality) |
| `TRIP_BASE_URL` | `https://hk.trip.com` | Market site |
| `TRIP_LOCALE` | `en_hk` | Locale query param |
| `TRIP_CURRENCY` | `HKD` | Prices in HKD |
| `HEADLESS` | `true` | Hide Chromium while scraping |
| `FAST_MODE` | `true` | Skip extra LLM ranking of flight/hotel/car candidates |
| `LLM_RANK_CANDIDATES` | `false` | Set `true` (+ `FAST_MODE=false`) to LLM-rank scrapes |
| `MAX_TOOL_ROUNDS` | `8` | Cap agent↔tool loops |
| `OLLAMA_NUM_CTX` | `4096` | Context window (lower = faster) |
| `OLLAMA_NUM_PREDICT` | `768` | Max new tokens per reply |
| `OLLAMA_TEMPERATURE` | `0.2` | Generation temperature |

Tip: if you want the browser to stay hidden while scraping, keep `HEADLESS=true` (default).

## Run from source

```bash
python -m travel_agent
```

Opens the **desktop GUI** (default). Trip.com scraping is **headless** unless you pass:

```bash
python -m travel_agent --show-browser
```

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

## Windows EXE

### Run the built app

Double-click:

```text
dist\VoyageApp\Voyage.exe
```

Still required on the PC (not bundled inside the EXE):

1. [Ollama](https://ollama.com/) + `ollama pull qwen2.5:3b`
2. Playwright Chromium in the user cache, **or** installed Chrome / Edge

Chromium install (once):

```bat
set PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright
python -m playwright install chromium
```

Voyage reads `.env` next to the EXE (`dist\VoyageApp\.env`). Copy from `.env.example` if missing.

### Build / rebuild

```bat
build_exe.bat
```

This installs deps, ensures Chromium is in `%LOCALAPPDATA%\ms-playwright`, and runs PyInstaller.

**Important:** close any running `Voyage.exe` before rebuilding — Windows locks `dist\VoyageApp` and the build will fail with Access Denied.

Output:

```text
dist\VoyageApp\Voyage.exe
```

Ignore older folders such as `dist\Voyage\` or `dist\VoyageRelease\` if present — use **VoyageApp** only.

## Typical agent flow

1. **`propose_trip_route`** — choose fly-into / fly-out airports and hotel cities for the trip length  
2. **`plan_trip`** — live Trip.com search for flights + hotels (+ cars if **Rent a car** is checked) + attractions  
3. GUI renders structured cards from scraped fields and trusted booking URLs  

Supporting tools (used as needed): `search_flights`, `search_hotels`, `search_cars`, `search_trains`, `search_transfers`, `search_attractions`.

### Hotels

- List results → pick a property (cheapest/first in `FAST_MODE`, or LLM when ranking is on) → booking URL is `/hotels/detail/?hotelId=…`
- Name / score / photo / price come from the matching list card and/or HTTP detail HTML (Playwright often cannot open detail pages due to sign-in redirects)
- Multi-city trips search **each stay city** separately so cards keep their own hotel link, name, and image

### Attractions & day schedule

- Searches Trip.com **Attractions** tab (`things-to-do/list`)
- Opens attraction detail pages for **name, photo, address, open hours, recommended visit time**
- Builds a day timetable with specific places (not generic “Attractions & Tours” labels)
- Each attraction is scheduled at most once across the trip

### Car rental (optional)

- Only when **Rent a car** is enabled (otherwise car search is skipped)
- Scrapes Trip.com list cards (`.vehicle-item-fuse`) for name, specs, vendor, price, and `img.vehicle-item-fuse__image`
- **View deal** opens the scraped `/carrentals/detail` link

### Budget

- Budget from the wizard is passed into the planning prompt and `plan_trip`
- Tool output includes a low-end estimate (transport + hotel [+ car/transfers]) vs your budget
- With `FAST_MODE=true`, candidate picks prefer cheapest/first heuristics rather than full LLM ranking against budget

## Project layout

```
travel_agent/             # Main package
  agent.py               #   Ollama tool-calling loop + booking_links merge
  browser_tools.py       #   Playwright Trip.com tools (flights, hotels, cars, plan_trip)
  gui.py                 #   Desktop wizard + itinerary board (tkinter + Pillow)
  cli.py                 #   Entry: GUI by default, --cli / -q for terminal
  config.py              #   Settings from .env (+ frozen EXE paths)
  planner_query.py       #   Prompt builder from GUI context
  itinerary_parse.py     #   Parse LLM text into structured offers / day blocks
  trip_urls.py           #   Normalize / trust Trip.com URLs
  regions.py             #   Multi-city regional routes + night alignment
  llm_select.py          #   Ranking / attraction day arrangement
  destination_guides.py  #   Day-idea fallbacks when scrape is thin
  attraction_images.py   #   Attraction / timetable image helpers
  places.py              #   City ↔ airport / hotel city helpers
  pricing.py             #   HKD price parse + comparisons
  airline_names.py       #   Airline code / logo helpers
  ollama_lifecycle.py    #   Start / ensure Ollama + model availability

tests/                   # Automated tests
  run_trip_examples.py   #   Fast logic suite (50 scenarios, no browser)
  run_trip_examples_live.py  # Full live Trip.com scrape (slow, hours)
  trip_examples_50.py    #   Scenario definitions
  test_llm_select.py     #   Unit tests for ranking/selection helpers
  test_card_parse.py     #   Itinerary card parsing assertions
  test_structured_cards.py   # Structured tool card vs bad LLM text
  test_flight_row_parse.py   # Flight row parser (uses fixture from scripts/)
  test_hotel_link.py     #   Hotel URL resolution (live browser)

scripts/                 # Ad-hoc debug helpers (require live browser)
  debug_flight_scrape.py #   Capture flight body text fixture
  debug_return_scrape.py #   Scrape return-leg after "Select" click
  debug_hotel_image.py   #   Inspect hotel photo URLs

voyage.py                # Frozen EXE entry point (PyInstaller)
voyage.spec              # PyInstaller build definition
build_exe.bat            # One-click Windows EXE build → dist\VoyageApp\
setup.py                 # pip-installable package metadata
```

## Tests

```bash
# Fast logic checks — 50 scenarios, no browser (seconds)
python -m tests.run_trip_examples

# Unit tests (no browser)
python -m tests.test_card_parse
python -m tests.test_structured_cards
python -m tests.test_llm_select

# Flight row parser (needs fixture from scripts/debug_flight_scrape.py)
python -m tests.test_flight_row_parse

# Live Trip.com scrape for the full example set (slow — hours)
python -m tests.run_trip_examples_live
python -m tests.run_trip_examples_live --resume
python -m tests.run_trip_examples_live --ids 1,27,50
python -m tests.run_trip_examples_live --mode scrape
```

Results go under `tests/live_results/` (`progress.jsonl`, transcripts, `summary.json`).
This folder is git-ignored.

## Notes

- Live prices and booking links come from Trip.com scrape / tool output — not model hallucination.
- Closing the app does **not** stop Ollama (so other tools can keep using it).
- Playwright browsers are loaded from `%LOCALAPPDATA%\ms-playwright` (not from inside the EXE `_internal` folder).
- If Chromium is missing, Voyage tries installed **Chrome** or **Edge**.
- No embedded map; use day cards plus Open Trip.Planner / booking links.
- Default model: `qwen2.5:3b` (`OLLAMA_MODEL` or `-m` to override).

### Ollama troubleshooting (Windows)

If you see `WinError 10054`, `502`, or “Ollama failed during generation”:

1. Use IPv4 in `.env`: `OLLAMA_HOST=http://127.0.0.1:11434` (not `localhost`)
2. Run the diagnostic: `python scripts/diagnose_ollama.py`
3. Confirm Ollama works: `ollama run qwen2.5:3b "Say hi"`

Voyage auto-starts Ollama when needed; you do **not** need a second `ollama serve` if port 11434 is already in use.

## License / attribution

Trip.com and Trip.Planner are trademarks of their respective owners. This project is an independent local client that automates a browser session against the public Hong Kong site for personal planning use.
