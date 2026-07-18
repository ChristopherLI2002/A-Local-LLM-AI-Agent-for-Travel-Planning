"""Ollama-powered travel agent with Trip.com browser tools."""

from __future__ import annotations

import json
from typing import Any, Callable

import ollama

from travel_agent.browser_tools import TOOL_DEFINITIONS, TripBrowser, dispatch_tool
from travel_agent.config import settings

SYSTEM_PROMPT = """You are a travel planning agent for Trip.com Hong Kong.

You have a real browser on https://hk.trip.com (locale en_hk, currency HKD).
Your core jobs are:
1) Transport comparison (flights, trains, airport transfers — not flights only)
2) Hotel price comparison
3) Full travel planning (multi-transport + hotels + optional car rental + itinerary + budget)

Tool choice:
- compare_flight_prices: when user wants cheapest flight dates/routes
- compare_hotel_prices: when user wants hotel price comparison across dates or cities
- plan_trip: when user asks to plan a trip / vacation / itinerary
  (include trains + airport transfers by default; set rent_car=true if they need a rental car;
   respect include_flights / include_trains / include_transfers flags from the user)
- search_trains: rail alternative to flying
- search_transfers: airport↔hotel ground transfers
- search_cars: rental car / car hire / self-drive / road trip
- search_flights / search_hotels: single targeted lookup
- browse_url / click_text / get_page_summary: only if needed to dig into a page

Guidelines:
- Prefer IATA codes for flights (hkg, tpe, tyo/nrt/hnd, sin, bkk, icn, mnl, etc.).
- Dates must be YYYY-MM-DD and in the future.
- Always ground prices in tool output. Never invent live fares.
- Present comparisons as ranked tables: cheapest first, savings called out.
- For travel plans, compare relevant transport modes (flights vs trains) and include hotels, budget, day-by-day outline, booking next steps.
- If a rental car is needed, search cars and include the Car rental search URL as a clickable link.
- Always pass through Trip.com search URLs from tool output (Flight / Train / Airport transfer / Hotel / Car rental) as plain URLs — never invent URLs.
- Write plain text answers (no HTML). Use short headings and bullet lists.
- If page text is sparse, say what is uncertain and suggest a narrower search.
- Keep answers practical and concise; use HKD unless the page shows otherwise.
"""


class TravelAgent:
    def __init__(
        self,
        model: str | None = None,
        on_tool_start: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_end: Callable[[str, str], None] | None = None,
    ) -> None:
        self.model = model or settings.ollama_model
        self.client = ollama.Client(host=settings.ollama_host)
        self.browser = TripBrowser()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end

    def start(self) -> None:
        self.browser.start()

    def close(self) -> None:
        self.browser.close()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        for _ in range(settings.max_tool_rounds):
            response = self.client.chat(
                model=self.model,
                messages=self.messages,
                tools=TOOL_DEFINITIONS,
            )
            message = response["message"]
            self.messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return (message.get("content") or "").strip() or "(No response from model.)"

            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = dict(raw_args or {})

                if self.on_tool_start:
                    self.on_tool_start(name, args)

                result = dispatch_tool(self.browser, name, args)

                if self.on_tool_end:
                    preview = result if len(result) <= 500 else result[:500] + "..."
                    self.on_tool_end(name, preview)

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": result,
                    }
                )

        # Final pass without tools if we hit the round limit
        response = self.client.chat(model=self.model, messages=self.messages)
        message = response["message"]
        self.messages.append(message)
        return (message.get("content") or "").strip() or (
            "I reached the tool-call limit. Please refine your request."
        )
