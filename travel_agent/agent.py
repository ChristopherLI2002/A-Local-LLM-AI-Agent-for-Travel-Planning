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
1) Flight price comparison — actually run it and recommend a pick
2) Hotel price comparison — actually run it and recommend a pick
3) Full travel planning (multi-transport + hotels + optional car rental + itinerary + budget)

Tool choice:
- plan_trip: primary tool for trip planning (it compares flight dates + hotel dates live)
- compare_flight_prices: extra flight date/route ranking when needed
- compare_hotel_prices: extra hotel city/date ranking when needed
- search_trains / search_transfers / search_cars / search_flights / search_hotels: targeted lookups
- browse_url / click_text / get_page_summary: only if needed to dig into a page

Critical behavior:
- When planning a trip, ALWAYS recommend one flight and one hotel with HKD prices AND their Trip.com links from tool output.
- Put Recommended flight and Recommended hotel (with links) at the top of the final answer.
- NEVER tell the user "next step: compare flights/hotels" — you must compare yourself first.
- Prefer IATA codes when searching flights if the user gave city names (e.g. Hong Kong->hkg, Paris->cdg/par).
- Dates must be YYYY-MM-DD and in the future.
- Always ground prices in tool output. Never invent live fares or URLs.
- Present comparisons ranked cheapest-first with savings called out.
- Pass through Trip.com search/detail URLs from tool output as plain URLs.
- Write plain text (no HTML). Use short headings and bullet lists.
- Keep answers practical; use HKD unless the page shows otherwise.
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
