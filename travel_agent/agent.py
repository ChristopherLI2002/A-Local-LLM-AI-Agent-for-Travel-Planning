"""Ollama-powered travel agent with Trip.com browser tools."""

from __future__ import annotations

import json
from typing import Any, Callable

import ollama

from travel_agent.browser_tools import TOOL_DEFINITIONS, TripBrowser, dispatch_tool
from travel_agent.config import settings
from travel_agent.trip_urls import extract_booking_urls, is_trusted_hotel_detail_url, score_booking_url

SYSTEM_PROMPT = """You are Voyage — a Trip.Planner-style AI travel concierge for Trip.com Hong Kong (hk.trip.com, HKD).

Like Trip.Planner, you turn three inputs (destination, duration, travel style) into a
personalised itinerary with bookable flight and hotel picks.

Tools:
- plan_trip: primary (compares flight/hotel dates, returns hk.trip.com links + prices)
- compare_flight_prices / compare_hotel_prices: extra ranking if needed
- search_flights / search_hotels / search_trains / search_transfers / search_cars
- browse_url / click_text / get_page_summary

Output rules (plain text, no HTML):
1) Start with "Recommended flight" including airline, from/to airports, depart/arrive times,
   duration, stops, baggage if known, HKD price, and exact hk.trip.com URL
2) Then "Recommended hotel" with hotel name, stars, score/reviews, location, features,
   room/beds, nightly + total HKD, and exact hk.trip.com hotel DETAIL URL (/hotels/detail/?hotelId=...)
3) Then "Day-by-day itinerary" with "Day 1:", "Day 2:", ... shaped by travel style
4) End with "Budget snapshot"

Hard rules:
- Prefer Recommended hotel detail link / Hotel option link from tools (not list/search URLs).
- NEVER invent www.trip.com generic /search URLs or fake prices.
- Only use https://hk.trip.com/... links that appear in tool results.
- Round-trip flights; hotel stay matches full trip length.
- Travel style must change the itinerary pace (Culture vs Food vs Family, etc.).
- When refining, keep the same section headings so the UI can re-parse the plan.
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
        self.booking_links: dict[str, str] = {"flight": "", "hotel": "", "hotel_name": ""}

    def start(self) -> None:
        self.browser.start()

    def close(self) -> None:
        self.browser.close()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.booking_links = {"flight": "", "hotel": "", "hotel_name": ""}

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.booking_links = {"flight": "", "hotel": "", "hotel_name": ""}

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

                found = extract_booking_urls(result)
                if found.get("flight"):
                    self.booking_links["flight"] = found["flight"]
                if found.get("hotel"):
                    new_h = found["hotel"]
                    old_h = self.booking_links.get("hotel", "")
                    if is_trusted_hotel_detail_url(new_h):
                        if not is_trusted_hotel_detail_url(old_h) or (
                            score_booking_url(new_h, "hotel")
                            >= score_booking_url(old_h, "hotel")
                        ):
                            self.booking_links["hotel"] = new_h
                if found.get("hotel_name"):
                    self.booking_links["hotel_name"] = found["hotel_name"]
                if found.get("flight_price"):
                    self.booking_links["flight_price"] = found["flight_price"]
                if found.get("flight_option"):
                    self.booking_links["flight_option"] = found["flight_option"]
                if found.get("hotel_price"):
                    self.booking_links["hotel_price"] = found["hotel_price"]
                if found.get("hotel_total"):
                    self.booking_links["hotel_total"] = found["hotel_total"]
                for key in (
                    "flight_airline",
                    "flight_depart",
                    "flight_arrive",
                    "flight_from",
                    "flight_to",
                    "flight_duration",
                    "flight_stops",
                    "hotel_stars",
                    "hotel_score",
                    "hotel_location",
                    "hotel_reviews",
                ):
                    if found.get(key):
                        self.booking_links[key] = found[key]

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
