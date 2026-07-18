# Trip.com Travel Agent (Ollama + Browser)

Local AI travel planner that uses an **Ollama** LLM and a real Chromium browser on **[Trip.com Hong Kong](https://hk.trip.com/?locale=en_hk&curr=HKD)** for:

- **Flight price comparison** (dates / alternate destinations)
- **Hotel price comparison** (cities / check-in dates)
- **Travel planning** (flights + hotels + budget + day outline)

## Prerequisites

1. [Ollama](https://ollama.com/) running locally
2. A tool-capable model, e.g. `qwen2.5:7b` (already supports tools)
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

## Run

### HTML trip wizard (recommended)

Guided UI that asks **destination → travel date → budget**, then plans the trip:

```bash
python -m travel_agent --web
```

Open [http://127.0.0.1:7860](http://127.0.0.1:7860). Headless browser:

```bash
python -m travel_agent --web --headless
```

### Terminal chat

Interactive chat (opens a visible Trip.com browser window):

```bash
python -m travel_agent
```

Single question:

```bash
python -m travel_agent -q "Compare HKG to TPE flights on 2026-08-15,2026-08-20,2026-08-25"
```

Headless examples:

```bash
python -m travel_agent --headless -q "Compare hotel prices in Taipei for check-in 2026-08-20 vs 2026-08-27"
python -m travel_agent --headless -q "Plan a round-trip from HKG to TYO, Aug 20-25, 2 adults, budget 15000 HKD, food and museums"
```

Use another Ollama model:

```bash
python -m travel_agent -m qwen3.5:9b
```

## Chat commands

| Command | Action |
|---------|--------|
| `/exit` | Quit |
| `/reset` | Clear conversation memory |
| `/home` | Reload Trip.com Hong Kong home |

## How it works

1. You ask for a comparison or a trip plan in natural language.
2. The Ollama model calls tools such as `compare_flight_prices`, `compare_hotel_prices`, or `plan_trip`.
3. Playwright searches `hk.trip.com` (locale `en_hk`, currency `HKD`) and parses visible HKD prices.
4. The model returns a ranked comparison or a full itinerary with budget notes.

## Project layout

```
web/
  index.html        # Multi-step trip wizard UI
  styles.css
  app.js
travel_agent/
  agent.py          # Ollama tool-calling loop
  browser_tools.py  # Trip.com Playwright tools (+ compare/plan)
  pricing.py        # HKD price parse + comparison tables
  cli.py            # Rich terminal UI (+ --web)
  web_server.py     # Flask server for the HTML wizard
  config.py         # Settings from .env
```

## Notes

- Live prices come from the Trip.com page the browser opens; the model is instructed not to invent fares.
- Trip.com pages are dynamic; if a search returns sparse text, ask the agent to retry or refine the route/date.
- Default model: `qwen2.5:7b`. Override with `OLLAMA_MODEL` or `-m`.
