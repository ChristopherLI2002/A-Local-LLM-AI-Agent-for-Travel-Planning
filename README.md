# Voyage — Trip.Planner-style Travel Agent

Local AI travel concierge inspired by [Trip.com Trip.Planner](https://hk.trip.com/webapp/tripmap/tripplanner?source=t_online_homepage&locale=en-HK&curr=HKD). Uses **Ollama** plus a Chromium session on **hk.trip.com** for live flights and hotels.

## Flow (like Trip.Planner)

1. **Destination** — where you’re going (from Hong Kong by default)
2. **Duration** — nights / dates (default: tomorrow + 7 nights)
3. **Travel style** — First-time, Culture, Food, Family, Relaxed, Adventure
4. **Generate itinerary** — recommended flight + hotel with `hk.trip.com` links, plus a day-by-day plan
5. **Refine with AI** — chat dock to tweak the plan without leaving the board

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

## Run

```bash
python -m travel_agent
```

Trip.com scraping runs **headless** by default (no Chromium popup). To watch the browser:

```bash
python -m travel_agent --show-browser
```

Header button **Open Trip.Planner** launches the official product:  
https://hk.trip.com/webapp/tripmap/tripplanner?source=t_online_homepage&locale=en-HK&curr=HKD

Terminal mode:

```bash
python -m travel_agent --cli
```

## Project layout

```
travel_agent/
  gui.py             # Trip.Planner-style wizard + itinerary board
  agent.py           # Ollama tool-calling loop
  browser_tools.py   # Trip.com Playwright tools
  planner_query.py   # Prompt builder (destination / duration / style)
  itinerary_parse.py # Split plan into flight / hotel / day cards
  places.py          # City → airport code mapping
  pricing.py         # HKD price parse + comparisons
  cli.py             # Default → GUI; --cli for terminal
  config.py
```

## Notes

- Live prices and booking links come from Trip.com page/tool output; the model must not invent `www.trip.com` URLs.
- No embedded map (desktop limitation); use day-by-day cards + Open Trip.Planner / booking links instead.
- Default model: `qwen2.5:7b` (`OLLAMA_MODEL` or `-m` to override).
