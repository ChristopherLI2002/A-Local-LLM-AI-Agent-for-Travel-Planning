# Trip.com Travel Agent (Ollama + Desktop App)

Local AI travel planner with a **Windows desktop window**. Uses **Ollama** and a Chromium browser on **[Trip.com Hong Kong](https://hk.trip.com/?locale=en_hk&curr=HKD)** for:

- **Flight price comparison**
- **Hotel price comparison**
- **Travel planning** (flights, trains, transfers, hotels, budget, itinerary)

## Prerequisites

1. [Ollama](https://ollama.com/) running locally
2. A tool-capable model, e.g. `qwen2.5:7b`
3. Python 3.10+

```bash
ollama serve
ollama pull qwen2.5:7b
```

## Setup

```bash
cd "d:\An AI Agent for travel planning"
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

## Run (desktop window)

```bash
python -m travel_agent
```

Opens the **Voyage** app:

- **Trip planner** — destination, dates, budget, transport options → live plan
- **Chat** — flight/hotel comparisons and follow-ups

Hide the Trip.com Playwright window:

```bash
python -m travel_agent --headless
```

Other model:

```bash
python -m travel_agent -m qwen3.5:9b
```

## Terminal (optional)

```bash
python -m travel_agent --cli
python -m travel_agent -q "Compare HKG to TPE flights on 2026-08-20,2026-08-27"
```

## How it works

1. You enter trip details (or chat) in the desktop window.
2. Ollama calls tools (`plan_trip`, `compare_flight_prices`, `compare_hotel_prices`, …).
3. Playwright searches `hk.trip.com` and returns live page text / prices.
4. The plan appears as plain text; Trip.com URLs are clickable in the window.

## Project layout

```
travel_agent/
  gui.py            # Desktop window (tkinter)
  agent.py          # Ollama tool-calling loop
  browser_tools.py  # Trip.com Playwright tools
  planner_query.py  # Trip-plan prompt builder
  pricing.py        # HKD price parse + comparison tables
  cli.py            # Default → GUI; --cli for terminal
  config.py         # Settings from .env
```

## Notes

- No HTML/web server — the UI is a native window.
- Live prices come from Trip.com; the model is instructed not to invent fares.
- Default model: `qwen2.5:7b`. Override with `OLLAMA_MODEL` or `-m`.
