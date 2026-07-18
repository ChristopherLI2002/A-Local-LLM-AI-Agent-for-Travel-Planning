"""Ollama-powered travel agent with Trip.com browser tools."""

from __future__ import annotations

import json
from typing import Any, Callable

import ollama

from travel_agent.browser_tools import TOOL_DEFINITIONS, TripBrowser, dispatch_tool
from travel_agent.config import settings

SYSTEM_PROMPT = """You are a travel planning agent for Trip.com Hong Kong (hk.trip.com, HKD).

Your jobs:
1) Compare flights and recommend ONE flight with its live Trip.com link
2) Compare hotels and recommend ONE hotel with its live Trip.com link
3) Build a full trip plan (transport + hotels + budget + itinerary)

Tools:
- plan_trip: primary planner (compares flight/hotel dates and returns recommended links)
- compare_flight_prices / compare_hotel_prices: extra ranking if needed
- search_flights / search_hotels / search_trains / search_transfers / search_cars
- browse_url / click_text / get_page_summary

Hard rules:
- ALWAYS put Recommended flight and Recommended hotel FIRST, each with:
  date(s), price in HKD if available, and the exact Trip.com URL from tool output.
- Prefer "Canonical search URL" or "Flight search URL" / "Hotel search URL" from tools.
- NEVER invent URLs. NEVER use www.trip.com generic /search links you made up.
- Only use https://hk.trip.com/... links that appear in tool results.
- If tool output says "Parsed prices: NONE", say prices were unavailable on the live page
  and still provide the tool's search URL — do not fabricate prices or alternate sites.
- City names are fine; tools map them (Hong Kong->HKG, Paris->PAR/CDG).
- Round-trip flights and hotel stays must match the user's dates (usually 1 week).
- Plain text only. No HTML. No markdown link invention beyond pasting tool URLs.
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

        response = self.client.chat(model=self.model, messages=self.messages)
        message = response["message"]
        self.messages.append(message)
        return (message.get("content") or "").strip() or (
            "I reached the tool-call limit. Please refine your request."
        )
