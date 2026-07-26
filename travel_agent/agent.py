"""Ollama-powered travel agent with Trip.com browser tools."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import ollama

from travel_agent.browser_tools import TOOL_DEFINITIONS, TripBrowser, dispatch_tool
from travel_agent.config import resolve_ollama_model, settings
from travel_agent.trip_urls import extract_booking_urls, is_trusted_hotel_detail_url, score_booking_url

SYSTEM_PROMPT = """You are Voyage — a Trip.Planner-style AI travel concierge for Trip.com Hong Kong (hk.trip.com, HKD).

Like Trip.Planner, you turn destination, duration, and travel style into a
personalised itinerary with bookable flight and hotel picks.

Workflow (STRICT order):
1) FIRST call propose_trip_route — decide fly-into city/airport, fly-out city/airport,
   nights per city, and the rough transfer path. Do NOT search Trip.com yet.
2) THEN call plan_trip (preferred) — or search_flights / search_hotels — using the
   arrive_airport / return_airport (and stay cities) from the rough route.
3) Write the final itinerary from live tool results only.

Tools:
- propose_trip_route: rough route only (required first)
- plan_trip: primary live scrape (flights + hotels + links)
- compare_flight_prices / compare_hotel_prices: extra ranking if needed
- search_flights / search_hotels / search_trains / search_transfers / search_cars
- search_attractions: named sights from hk.trip.com/things-to-do
- browse_url / click_text / get_page_summary

Output rules (plain text, no HTML):
1) Optionally open with a short "Rough route" blurb (arrive/return hubs + city order)
2) "Recommended flight" — airline, from/to IATA airports, times, duration, stops,
   baggage if known, HKD price, exact hk.trip.com URL
3) "Recommended hotel" — name, stars, score/reviews, location, features, room/beds,
   nightly + total HKD, exact hotel DETAIL URL (/hotels/detail/?hotelId=...)
   For multi-city routes, one hotel block per stay city. Never use a hotels/list
   search URL as the booking link when a detail URL exists.
4) "Day-by-day itinerary" with EXACT lines "Day 1:", "Day 2:", ... (one block per
   trip day). Timetable with clock times and named places/restaurants only.
5) "Budget snapshot"

Hard rules:
- NEVER call plan_trip / search_flights / search_hotels before propose_trip_route
  in the same user request.
- When the user asks for a rental car / rent_car=true, pass rent_car=true to plan_trip.
- Prefer Recommended hotel detail link / Hotel option link from tools.
- Type city names with spaces (San Francisco), never with '+' (San+Francisco).
- NEVER invent www.trip.com generic /search URLs or fake prices.
- Only use https://hk.trip.com/... links that appear in tool results.
- Match hotels to the rough-route stay windows (not always the full trip in one city).
- Travel style must change itinerary pace (Culture vs Food vs Family, etc.).
- When refining, keep the same section headings so the UI can re-parse the plan.
- Do NOT dump raw tool tables as the final answer — rewrite into the sections above.
- Do NOT use vague lines like "local dinner and unwind" — name places.
- For regions like California / Florida, multi-city + open-jaw
  (e.g. SFO in / LAX out, or MIA in / MCO out).
- Use search_attractions for named sights in each city.
"""


class TravelAgent:
    def __init__(
        self,
        model: str | None = None,
        on_tool_start: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_end: Callable[[str, str], None] | None = None,
    ) -> None:
        preferred = model or settings.ollama_model
        try:
            self.model = resolve_ollama_model(preferred, host=settings.ollama_host)
        except RuntimeError:
            self.model = preferred
        self.client = ollama.Client(host=settings.ollama_host)
        self.browser = TripBrowser()
        self.browser.llm_model = self.model
        self.browser.llm_host = settings.ollama_host
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        self.booking_links: dict[str, str] = {
            "flight": "",
            "hotel": "",
            "hotel_name": "",
            "car": "",
            "car_name": "",
        }
        # GUI / prompt override — do not rely on the model to pass rent_car
        self.force_rent_car: bool = False

    def start(self) -> None:
        try:
            self.model = resolve_ollama_model(self.model, host=settings.ollama_host)
            self.browser.llm_model = self.model
        except RuntimeError:
            pass
        self.browser.start()

    def close(self) -> None:
        self.browser.close()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.booking_links = {
            "flight": "",
            "hotel": "",
            "hotel_name": "",
            "car": "",
            "car_name": "",
        }
        self.force_rent_car = False
        self.browser.last_proposed_route = None
        self.browser.last_plan_flight_card = {}
        self.browser.last_flight_card = {}
        self.browser.last_car_card = {}
        self.browser.last_car_detail_url = ""
        self.browser.last_attraction_day_plan = []

    def _ensure_model(self) -> None:
        """Fail fast with an actionable message when Ollama has no usable model."""
        self.model = resolve_ollama_model(self.model, host=settings.ollama_host)
        self.browser.llm_model = self.model

    def _enforce_trip_length_args(self, name: str, args: dict) -> None:
        """Overwrite tool args so hotels/route never shrink below the GUI trip length."""
        if name not in {
            "plan_trip",
            "propose_trip_route",
            "search_hotels",
            "compare_hotel_prices",
            "search_flights",
            "compare_flight_prices",
            "search_cars",
        }:
            return
        ctx = self.browser.selection_context
        ctx_n = int(getattr(ctx, "nights", 0) or 0)
        ctx_in = (getattr(ctx, "checkin", "") or "").strip()
        ctx_out = (getattr(ctx, "checkout", "") or "").strip()
        if not ctx_n and not ctx_in and not ctx_out:
            return

        if ctx_in:
            if name in {"plan_trip", "search_flights", "compare_flight_prices"}:
                args["depart_date"] = ctx_in
            if name in {"search_hotels", "compare_hotel_prices"}:
                args["checkin"] = ctx_in
            if name == "propose_trip_route":
                args["depart_date"] = ctx_in
            if name == "search_cars":
                args["pickup_date"] = args.get("pickup_date") or ctx_in

        if ctx_out:
            if name in {"plan_trip", "search_flights", "compare_flight_prices"}:
                args["return_date"] = ctx_out
            if name in {"search_hotels", "compare_hotel_prices"}:
                args["checkout"] = ctx_out
            if name == "search_cars":
                args["dropoff_date"] = args.get("dropoff_date") or ctx_out

        if name == "propose_trip_route" and ctx_n:
            try:
                arg_n = int(args.get("nights") or 0)
            except (TypeError, ValueError):
                arg_n = 0
            if arg_n < ctx_n:
                args["nights"] = ctx_n

        # plan_trip: if model return span is shorter than GUI nights, force checkout
        if name == "plan_trip" and ctx_n and ctx_in and ctx_out:
            args["depart_date"] = ctx_in
            args["return_date"] = ctx_out

    def _apply_plan_flight_card(self) -> None:
        """Copy frozen open-jaw / plan flight scrape into booking_links."""
        plan_card = getattr(self.browser, "last_plan_flight_card", None) or {}
        for k, v in plan_card.items():
            if not v or not str(k).startswith("flight"):
                continue
            cur = self.booking_links.get(k) or ""
            if not cur or cur in {"--:--", "See Trip.com", "n/a"}:
                self.booking_links[k] = v
            elif k in {
                "flight_depart",
                "flight_arrive",
                "flight_airline",
                "flight_return_depart",
                "flight_return_arrive",
                "flight_return_airline",
                "flight_price",
                "flight_duration",
                "flight_stops",
                "flight_return_duration",
                "flight_return_stops",
            }:
                self.booking_links[k] = v

    def _prefer_live_hotel_detail(self) -> None:
        """Always prefer Playwright /hotels/detail/?hotelId=… over list/search."""
        live_detail = (getattr(self.browser, "last_hotel_detail_url", "") or "").strip()
        if live_detail and is_trusted_hotel_detail_url(live_detail):
            self.booking_links["hotel"] = live_detail
        live_name = (getattr(self.browser, "last_hotel_name", "") or "").strip()
        if live_name and not live_name.lower().startswith(
            ("hotels in ", "recommended hotel")
        ):
            self.booking_links["hotel_name"] = live_name

    def _prefer_live_car_card(self) -> None:
        """Copy scraped carhire deal into booking_links when present."""
        live_car = (getattr(self.browser, "last_car_detail_url", "") or "").strip()
        if live_car and "/carrentals/detail" in live_car.lower():
            self.booking_links["car"] = live_car
        card = getattr(self.browser, "last_car_card", None) or {}
        if card.get("name"):
            self.booking_links["car_name"] = card["name"]
        for src, dest in (
            ("similar", "car_similar"),
            ("vendor", "car_vendor"),
            ("score", "car_score"),
            ("reviews", "car_reviews"),
            ("seats", "car_seats"),
            ("fuel", "car_fuel"),
            ("pickup_note", "car_pickup_note"),
            ("cancellation", "car_cancellation"),
            ("mileage", "car_mileage"),
            ("payment", "car_payment"),
            ("insurance", "car_insurance"),
            ("price_label", "car_price"),
            ("total_label", "car_total"),
            ("image_url", "car_image"),
            ("location", "car_location"),
            ("pickup_date", "car_pickup"),
            ("dropoff_date", "car_dropoff"),
            ("url", "car"),
        ):
            if card.get(src):
                self.booking_links[dest] = card[src]

    def chat(self, user_message: str) -> str:
        self._ensure_model()
        self.messages.append({"role": "user", "content": user_message})
        self.booking_links = {
            "flight": "",
            "hotel": "",
            "hotel_name": "",
            "car": "",
            "car_name": "",
        }
        route_ready = self.browser.last_proposed_route is not None
        want_car = bool(self.force_rent_car) or bool(
            re.search(
                r"(?i)rent_car\s*=\s*true|modes:\s*[^\n]*rental car",
                user_message or "",
            )
        )

        for _ in range(settings.max_tool_rounds):
            try:
                response = self.client.chat(
                    model=self.model,
                    messages=self.messages,
                    tools=TOOL_DEFINITIONS,
                )
            except Exception as exc:
                msg = str(exc)
                if "not found" in msg.lower() or "404" in msg:
                    raise RuntimeError(
                        f"Ollama model '{self.model}' is not available. "
                        f"Run: ollama pull {self.model}"
                    ) from exc
                raise
            message = response["message"]
            self.messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                self._apply_plan_flight_card()
                self._prefer_live_hotel_detail()
                self._prefer_live_car_card()
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

                # Never let the model skip the car hire scrape when the UI asked for it
                if name == "plan_trip" and want_car:
                    args["rent_car"] = True
                # City/keyword args must be typed with spaces (not Los+Angeles)
                for key in ("hotel_city", "city", "location", "destination"):
                    if args.get(key):
                        from travel_agent.places import typed_place_name

                        args[key] = typed_place_name(str(args[key]))

                # Force full trip length from the GUI selection (model often uses 7 nights)
                self._enforce_trip_length_args(name, args)

                # Enforce rough-route-before-scrape when the model skips propose
                scrape_tools = {
                    "plan_trip",
                    "search_flights",
                    "search_hotels",
                    "compare_flight_prices",
                    "compare_hotel_prices",
                }
                if name in scrape_tools and not route_ready:
                    ctx = self.browser.selection_context
                    ctx_n = int(getattr(ctx, "nights", 0) or 0)
                    propose_args = {
                        "destination": args.get("destination")
                        or args.get("city")
                        or args.get("hotel_city")
                        or "destination",
                        "nights": ctx_n or args.get("nights") or 7,
                        "depart_date": args.get("depart_date")
                        or args.get("checkin")
                        or getattr(ctx, "checkin", "")
                        or "",
                        "origin": args.get("origin") or "Hong Kong",
                        "interests": args.get("interests") or "",
                    }
                    if args.get("depart_date") and args.get("return_date"):
                        try:
                            from datetime import date as _date

                            d0 = _date.fromisoformat(str(args["depart_date"])[:10])
                            d1 = _date.fromisoformat(str(args["return_date"])[:10])
                            span = max(1, (d1 - d0).days)
                            propose_args["nights"] = max(ctx_n, span) if ctx_n else span
                        except ValueError:
                            pass
                    if ctx_n and int(propose_args.get("nights") or 0) < ctx_n:
                        propose_args["nights"] = ctx_n
                    if self.on_tool_start:
                        self.on_tool_start("propose_trip_route", propose_args)
                    propose_result = dispatch_tool(
                        self.browser, "propose_trip_route", propose_args
                    )
                    route_ready = True
                    if self.on_tool_end:
                        preview = (
                            propose_result
                            if len(propose_result) <= 500
                            else propose_result[:500] + "..."
                        )
                        self.on_tool_end("propose_trip_route", preview)
                    # Keep protocol clean: do not invent a tool message; plan_trip
                    # output already includes the ROUGH TRIP ROUTE block.
                    if name == "plan_trip":
                        route = self.browser.last_proposed_route
                        if route is not None:
                            if not args.get("arrive_airport"):
                                args["arrive_airport"] = getattr(
                                    route, "arrive_airport", ""
                                )
                            if not args.get("return_airport"):
                                args["return_airport"] = getattr(
                                    route, "depart_airport", ""
                                )
                            if not args.get("hotel_city") and getattr(route, "stays", None):
                                args["hotel_city"] = route.stays[0].city

                if self.on_tool_start:
                    self.on_tool_start(name, args)

                result = dispatch_tool(self.browser, name, args)
                if name == "propose_trip_route":
                    route_ready = True

                found = extract_booking_urls(result)
                # Prefer frozen plan_trip card (open-jaw) over later search_flights scrapes
                plan_card = getattr(self.browser, "last_plan_flight_card", None) or {}
                live = getattr(self.browser, "last_flight_card", None) or {}
                if name == "plan_trip" and live:
                    prefer = live
                elif plan_card:
                    prefer = plan_card
                elif name in {"search_flights", "compare_flight_prices"}:
                    prefer = live
                else:
                    prefer = plan_card or live
                for k, v in prefer.items():
                    if not v or not str(k).startswith("flight"):
                        continue
                    cur = found.get(k) or ""
                    if not cur or cur in {"--:--", "See Trip.com", "n/a"}:
                        found[k] = v
                    elif k in {
                        "flight_depart",
                        "flight_arrive",
                        "flight_airline",
                        "flight_return_depart",
                        "flight_return_arrive",
                        "flight_return_airline",
                        "flight_price",
                        "flight_duration",
                        "flight_stops",
                        "flight_return_duration",
                        "flight_return_stops",
                    }:
                        found[k] = v
                if found.get("flight"):
                    self.booking_links["flight"] = found["flight"]
                if found.get("flight_return"):
                    self.booking_links["flight_return"] = found["flight_return"]
                if found.get("hotel"):
                    new_h = found["hotel"]
                    old_h = self.booking_links.get("hotel", "")
                    live_detail = (
                        getattr(self.browser, "last_hotel_detail_url", "") or ""
                    ).strip()
                    live_list = (
                        getattr(self.browser, "last_hotel_list_url", "") or ""
                    ).strip()
                    # Prefer Playwright hotel detail page over list/search
                    if live_detail and is_trusted_hotel_detail_url(live_detail):
                        new_h = live_detail
                    if is_trusted_hotel_detail_url(new_h):
                        if not is_trusted_hotel_detail_url(old_h) or (
                            score_booking_url(new_h, "hotel")
                            >= score_booking_url(old_h, "hotel")
                        ):
                            self.booking_links["hotel"] = new_h
                    elif not is_trusted_hotel_detail_url(old_h):
                        if "/hotels/list" in new_h.lower() and (
                            "cityid=" in new_h.lower() or "city=" in new_h.lower()
                        ):
                            self.booking_links["hotel"] = new_h
                        elif live_list:
                            self.booking_links["hotel"] = live_list
                if found.get("hotel_name"):
                    self.booking_links["hotel_name"] = found["hotel_name"]
                # Always win with the live detail page when Playwright found one
                self._prefer_live_hotel_detail()
                if found.get("car"):
                    self.booking_links["car"] = found["car"]
                if found.get("car_name"):
                    self.booking_links["car_name"] = found["car_name"]
                for key in (
                    "car_similar",
                    "car_vendor",
                    "car_score",
                    "car_reviews",
                    "car_seats",
                    "car_fuel",
                    "car_pickup_note",
                    "car_cancellation",
                    "car_mileage",
                    "car_payment",
                    "car_insurance",
                    "car_price",
                    "car_total",
                    "car_image",
                    "car_location",
                    "car_pickup",
                    "car_dropoff",
                ):
                    if found.get(key):
                        self.booking_links[key] = found[key]
                self._prefer_live_car_card()
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
                    "flight_airline_logo",
                    "flight_date",
                    "flight_return",
                    "flight_return_airline",
                    "flight_return_depart",
                    "flight_return_arrive",
                    "flight_return_from",
                    "flight_return_to",
                    "flight_return_duration",
                    "flight_return_stops",
                    "flight_return_airline_logo",
                    "flight_return_date",
                    "hotel_stars",
                    "hotel_score",
                    "hotel_location",
                    "hotel_reviews",
                    "hotel_image",
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
        self._apply_plan_flight_card()
        return (message.get("content") or "").strip() or (
            "I reached the tool-call limit. Please refine your request."
        )
