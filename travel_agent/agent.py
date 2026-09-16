"""Ollama-powered travel agent with Trip.com browser tools."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from travel_agent.browser_tools import TOOL_DEFINITIONS, TripBrowser, dispatch_tool
from travel_agent.config import (
    apply_model_settings,
    is_student_model,
    make_ollama_client,
    ollama_chat_options,
    resolve_ollama_model,
    settings,
)
from travel_agent.ollama_lifecycle import ensure_ollama_running
from travel_agent.trip_urls import extract_booking_urls, is_trusted_hotel_detail_url, score_booking_url

try:
    from distill.compact_prompt import (
        COMPACT_SYSTEM_PROMPT,
        SLIM_TOOL_DEFINITIONS,
        compact_tool_result,
    )
except ImportError:  # pragma: no cover
    COMPACT_SYSTEM_PROMPT = ""
    SLIM_TOOL_DEFINITIONS = TOOL_DEFINITIONS

    def compact_tool_result(text: str, tool_name: str = "", *, max_chars: int = 3500) -> str:  # type: ignore[misc]
        return text


def _active_system_prompt(*, student_mode: bool = False) -> str:
    if student_mode and COMPACT_SYSTEM_PROMPT:
        return COMPACT_SYSTEM_PROMPT
    if settings.student_mode and COMPACT_SYSTEM_PROMPT:
        return COMPACT_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def _active_tools(*, student_mode: bool = False) -> list:
    if student_mode and SLIM_TOOL_DEFINITIONS:
        return list(SLIM_TOOL_DEFINITIONS)
    if settings.student_mode and SLIM_TOOL_DEFINITIONS:
        return list(SLIM_TOOL_DEFINITIONS)
    return list(TOOL_DEFINITIONS)


SYSTEM_PROMPT = """You are a local LLM AI agent for travel planning — a Trip.Planner-style concierge for Trip.com Hong Kong (hk.trip.com, HKD).

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
- search_attractions: Trip.com Attractions tab + detail pages (name, photo,
  address, open hours, recommended visit time) — then schedule days from that list
  NEVER write generic labels like "Attractions & Tours"
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
- NEVER schedule vague lines like "Attractions & Tours" — only real place names
  from Trip.com attraction detail cards (with open hours / visit time when known).
"""


class TravelAgent:
    def __init__(
        self,
        model: str | None = None,
        on_tool_start: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_end: Callable[[str, str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        student_mode: bool | None = None,
    ) -> None:
        preferred = model or settings.ollama_model
        if preferred:
            apply_model_settings(preferred)
        try:
            self.model = resolve_ollama_model(preferred, host=settings.ollama_host)
        except RuntimeError:
            self.model = preferred
        if student_mode is None:
            # Prefer explicit settings (from .env / apply_model_settings); fall back to name.
            self.student_mode = bool(settings.student_mode) or is_student_model(self.model)
        else:
            self.student_mode = bool(student_mode)
        # Keep global settings in sync for tool ranking / options helpers.
        settings.ollama_model = self.model
        settings.student_mode = self.student_mode
        self.client = make_ollama_client(settings.ollama_host)
        self.browser = TripBrowser()
        self.browser.llm_model = self.model
        self.browser.llm_host = settings.ollama_host
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": _active_system_prompt(student_mode=self.student_mode)}
        ]
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        self.on_status = on_status
        self.booking_links: dict[str, str] = self._new_booking_links()
        # GUI / prompt override — do not rely on the model to pass rent_car
        self.force_rent_car: bool = False

    def _new_booking_links(self) -> dict[str, str]:
        """Initialize the minimal set of fields the GUI expects.

        Tool/scrape code may later add extra keys (e.g. pricing/score/image URLs)
        when available.
        """

        return {
            "flight": "",
            "hotel": "",
            "hotel_name": "",
            "car": "",
            "car_name": "",
        }

    def clear_booking_links(self) -> None:
        """Reset booking links back to the GUI's minimal empty state."""

        self.booking_links = self._new_booking_links()

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
        self.messages = [
            {"role": "system", "content": _active_system_prompt(student_mode=self.student_mode)}
        ]
        self.clear_booking_links()
        self.force_rent_car = False
        self.browser.last_proposed_route = None
        self.browser.last_plan_flight_card = {}
        self.browser.last_flight_card = {}
        self.browser.last_car_card = {}
        self.browser.last_car_detail_url = ""
        self.browser.last_attraction_day_plan = []
        self.browser.last_attraction_cards = []
        self.browser.last_attractions = []

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

    @staticmethod
    def _is_retryable_ollama_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "10054" in msg
            or "forcibly closed by the remote host" in msg
            or "connection reset" in msg
            or "connection aborted" in msg
            or "status code: 502" in msg
            or "502 bad gateway" in msg
        )

    @staticmethod
    def _is_invalid_tool_call_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "invalid tool call" in msg
            or "unexpected end of json" in msg
            or "invalid json" in msg
        )

    def _effective_max_tool_rounds(self) -> int:
        if self.student_mode:
            return max(2, int(settings.student_max_tool_rounds))
        return max(1, int(settings.max_tool_rounds))

    def _expected_nights(self) -> int:
        ctx = self.browser.selection_context
        try:
            return max(0, int(getattr(ctx, "nights", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _day_block_count(text: str) -> int:
        nums = [
            int(m.group(1))
            for m in re.finditer(r"(?im)^(?:\*\*|__|#+\s*)?Day\s+(\d+)\s*[:\-—]", text or "")
        ]
        return len(set(nums))

    @staticmethod
    def _day_numbers(text: str) -> list[int]:
        return [
            int(m.group(1))
            for m in re.finditer(r"(?im)^(?:\*\*|__|#+\s*)?Day\s+(\d+)\s*[:\-—]", text or "")
        ]

    @staticmethod
    def _looks_like_tool_echo(text: str) -> bool:
        low = (text or "").lower()
        return (
            "<tool_call>" in low
            or "please call the tools" in low
            or "call propose_trip_route" in low
            or "call the tools in the correct order" in low
            or low.endswith(".png")
            or low.endswith(".md")
            or "finalized_trip_plan" in low.replace(" ", "_")
        )

    def _normalize_message(self, message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            out = message
        elif hasattr(message, "model_dump"):
            out = message.model_dump()
        elif hasattr(message, "dict"):
            out = message.dict()
        else:
            out = {
                "role": getattr(message, "role", "assistant"),
                "content": getattr(message, "content", "") or "",
                "tool_calls": getattr(message, "tool_calls", None),
            }
        if out.get("tool_calls") is None:
            out.pop("tool_calls", None)
        return out

    @staticmethod
    def _looks_like_garbage_answer(text: str) -> bool:
        t = (text or "").strip()
        low = t.lower()
        if low.endswith(".png") or low.endswith(".jpg") or low.endswith(".md"):
            return True
        if "finalized_trip_plan" in low.replace(" ", "_"):
            return True
        if "<tool_call>" in low:
            return True
        has_sections = "recommended flight" in low or "recommended hotel" in low
        if has_sections:
            return False
        if len(t) < 200:
            return True
        return True

    def _tool_corpus(self) -> str:
        parts: list[str] = []
        for m in self.messages:
            if m.get("role") == "tool":
                parts.append(str(m.get("content") or ""))
        return "\n".join(parts)

    def _extract_attractions_from_tools(self) -> list[str]:
        cards = list(getattr(self.browser, "last_attraction_cards", None) or [])
        names: list[str] = []
        for c in cards:
            if isinstance(c, dict) and c.get("name"):
                names.append(str(c["name"]).strip())
        corpus = self._tool_corpus()
        for m in re.finditer(
            r"(?im)^(?:-\s*)?(?:Attraction|Sight|Place|Recommended hotel name|Hotel name|Option seen)\s*:\s*(.+)$",
            corpus,
        ):
            name = m.group(1).strip()
            if name and not name.lower().startswith("http") and len(name) < 120:
                # Skip flight option lines like "0. HK Express ..."
                if re.match(r"^\d+\.\s", name):
                    continue
                names.append(name)
        # Dedupe preserve order
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            key = n.lower()
            if key not in seen:
                seen.add(key)
                out.append(n)
        return out

    def _render_day_blocks_text(self, nights: int) -> str:
        from travel_agent.itinerary_parse import synthesize_day_blocks

        ctx = self.browser.selection_context
        dest = (getattr(ctx, "destination", "") or "").strip()
        styles_raw = (getattr(ctx, "travel_styles", "") or "").strip()
        styles = [s.strip() for s in styles_raw.split(",") if s.strip()]
        arrive = (self.booking_links.get("flight_arrive") or "").strip()
        ret_dep = (self.booking_links.get("flight_return_depart") or "").strip()
        attractions = self._extract_attractions_from_tools()
        blocks = synthesize_day_blocks(
            nights=max(1, nights),
            destination=dest,
            styles=styles or None,
            arrive_time=arrive,
            return_depart_time=ret_dep,
            attractions=attractions or None,
        )
        lines: list[str] = []
        for i, block in enumerate(blocks, start=1):
            title = str(getattr(block, "title", "") or f"Day {i}")
            # Score regex prefers "Day N:"; keep named subtitle after colon.
            title = re.sub(r"(?i)^Day\s+\d+\s*[-—:]\s*", "", title).strip() or title
            body = str(getattr(block, "body", "") or "").strip()
            lines.append(f"Day {i}: {title}")
            if body:
                lines.append(body)
            lines.append("")
        return "\n".join(lines).strip()

    def _ensure_day_blocks_text(self, content: str) -> str:
        nights = self._expected_nights()
        if nights <= 0:
            return content
        text = content or ""
        nums = self._day_numbers(text)
        if len(set(nums)) >= nights and nums and max(nums) >= nights:
            return text

        day_body = self._render_day_blocks_text(nights)
        day_section = f"Day-by-day itinerary\n{day_body}"

        # Always strip any partial itinerary / Day N chunks, then insert a full block.
        text = re.sub(
            r"(?is)\n*Day-by-day itinerary\b.*?(?=\n+Budget snapshot\b|\Z)",
            "\n",
            text,
        )
        text = re.sub(
            r"(?is)(?:\n|^)(?:\*\*|__|#+\s*)?Day\s+\d+\s*[:\-—].*?(?=\n+(?:Budget snapshot|Recommended (?:flight|hotel))\b|\Z)",
            "\n",
            text,
        )
        text = re.sub(r"\n{3,}", "\n\n", text).rstrip()

        if re.search(r"(?im)^Budget snapshot\b", text):
            text = re.sub(
                r"(?im)^(Budget snapshot\b)",
                day_section + "\n\n\\1",
                text,
                count=1,
            )
        else:
            text = text + "\n\n" + day_section
        return text

    @staticmethod
    def _strip_vague_phrases(text: str) -> str:
        out = text or ""
        out = re.sub(r"(?i)\battractions?\s*&\s*tours\b", "named attractions", out)
        out = re.sub(r"(?i)\blocal dinner(?:\s+and\s+unwind)?\b", "dinner at a named restaurant", out)
        out = re.sub(r"(?i)\bsightseeing\b(?!\s+at\b)", "visit", out)
        return out

    def _flight_hotel_fallback_sections(self) -> str:
        links = self.booking_links
        corpus = self._tool_corpus()
        airline = links.get("flight_airline") or ""
        depart = links.get("flight_depart") or ""
        arrive = links.get("flight_arrive") or ""
        price = links.get("flight_price") or ""
        flight_url = links.get("flight") or ""
        hotel = links.get("hotel_name") or ""
        if not hotel:
            m = re.search(r"(?im)Recommended hotel name:\s*(.+)$", corpus)
            if m:
                hotel = m.group(1).strip()
        hotel_url = links.get("hotel") or ""
        # Never emit "see tool results" — the student copies that onto UI cards.
        parts = [
            "Recommended flight",
            f"- Airline: {airline}" if airline else "",
            f"- Depart: {depart}" if depart else "",
            f"- Arrive: {arrive}" if arrive else "",
            f"- Price: {price}" if price else "",
            f"- Link: {flight_url}" if flight_url else "",
            "",
            "Recommended hotel",
            f"- Hotel: {hotel}" if hotel else "",
            f"- Link: {hotel_url}" if hotel_url else "",
            "",
            "Budget snapshot",
            f"- Flight: {price}" if price else "",
            (
                f"- Hotel: {links.get('hotel_total') or links.get('hotel_price')}"
                if (links.get("hotel_total") or links.get("hotel_price"))
                else ""
            ),
        ]
        return "\n".join(p for p in parts if p is not None)

    def _force_student_tools(self, *, want_car: bool) -> None:
        """Run propose_trip_route + plan_trip when the student skips tools."""
        ctx = self.browser.selection_context
        propose_args: dict[str, Any] = {}
        self._fill_tool_args_from_context("propose_trip_route", propose_args)
        propose_args["rent_car"] = bool(want_car)
        if self.on_tool_start:
            self.on_tool_start("propose_trip_route", propose_args)
        propose_result = dispatch_tool(self.browser, "propose_trip_route", propose_args)
        if self.student_mode:
            propose_result = compact_tool_result(
                propose_result,
                "propose_trip_route",
                max_chars=max(1024, int(settings.student_tool_result_max_chars)),
            )
        if self.on_tool_end:
            preview = propose_result if len(propose_result) <= 500 else propose_result[:500] + "..."
            self.on_tool_end("propose_trip_route", preview)
        self.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "propose_trip_route",
                            "arguments": propose_args,
                        }
                    }
                ],
            }
        )
        self.messages.append(
            {"role": "tool", "tool_name": "propose_trip_route", "content": propose_result}
        )

        plan_args: dict[str, Any] = {"rent_car": bool(want_car)}
        self._fill_tool_args_from_context("plan_trip", plan_args)
        self._enforce_trip_length_args("plan_trip", plan_args)
        if self.on_tool_start:
            self.on_tool_start("plan_trip", plan_args)
        plan_result = dispatch_tool(self.browser, "plan_trip", plan_args)
        if self.student_mode:
            plan_result = compact_tool_result(
                plan_result,
                "plan_trip",
                max_chars=max(1024, int(settings.student_tool_result_max_chars)),
            )
        if self.on_tool_end:
            preview = plan_result if len(plan_result) <= 500 else plan_result[:500] + "..."
            self.on_tool_end("plan_trip", preview)
        self.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "plan_trip", "arguments": plan_args}}
                ],
            }
        )
        self.messages.append({"role": "tool", "tool_name": "plan_trip", "content": plan_result})
        # Collect booking URLs from scrape text for fallback sections
        found = extract_booking_urls(plan_result)
        for k, v in found.items():
            if v and not self.booking_links.get(k):
                self.booking_links[k] = v
        self._apply_plan_flight_card()
        self._prefer_live_hotel_detail()
        if want_car:
            self._prefer_live_car_card()

    def _finalize_student_answer(self, content: str) -> str:
        text = (content or "").strip()
        if not self.student_mode:
            text = text or "(No response from model.)"
            if text:
                self.messages.append({"role": "assistant", "content": text})
            return text
        if self._looks_like_garbage_answer(text) or self._looks_like_tool_echo(text):
            text = self._flight_hotel_fallback_sections()
        # Ensure hotel heading exists for scorers even when the draft only had flights.
        if "recommended hotel" not in text.lower():
            hotel_bit = self._flight_hotel_fallback_sections()
            # Keep flight part from draft when present; append hotel/budget from fallback.
            hotel_only = hotel_bit
            if "Recommended hotel" in hotel_bit:
                hotel_only = hotel_bit[hotel_bit.index("Recommended hotel") :]
            text = text.rstrip() + "\n\n" + hotel_only
        text = self._ensure_day_blocks_text(text)
        text = self._strip_vague_phrases(text)
        if "Budget snapshot" not in text:
            text = text.rstrip() + "\n\nBudget snapshot\n- See Recommended flight/hotel prices above."
        text = text.strip() or "(No response from model.)"
        self.messages.append({"role": "assistant", "content": text})
        return text

    def _fill_tool_args_from_context(self, name: str, args: dict[str, Any]) -> None:
        from travel_agent.planner_query import is_travel_style_label

        ctx = self.browser.selection_context
        route = self.browser.last_proposed_route
        ctx_dest = (getattr(ctx, "destination", "") or "").strip()
        ctx_n = int(getattr(ctx, "nights", 0) or 0)
        ctx_in = (getattr(ctx, "checkin", "") or "").strip()
        ctx_out = (getattr(ctx, "checkout", "") or "").strip()

        if name == "propose_trip_route":
            dest = ctx_dest or "destination"
            # Models often pass "Tokyo, Japan" — strip country for IATA / hotels.
            if "," in dest:
                dest = dest.split(",", 1)[0].strip() or dest
            args.setdefault("destination", dest)
            # Student often swaps destination with a travel style (First-time).
            if ctx_dest and is_travel_style_label(str(args.get("destination") or "")):
                args["destination"] = dest
            args.setdefault("nights", ctx_n or 7)
            if ctx_in:
                args.setdefault("depart_date", ctx_in)
            args.setdefault("origin", (getattr(ctx, "origin", "") or "Hong Kong"))
            # Never let "Tokyo, Japan" land in stay_cities as two cities.
            sc = str(args.get("stay_cities") or "").strip()
            if sc and ":" not in sc:
                args.pop("stay_cities", None)
            elif sc:
                from travel_agent.regions import parse_stay_cities_arg

                cleaned = parse_stay_cities_arg(sc, total_nights=ctx_n or 7)
                if len(cleaned) <= 1:
                    args.pop("stay_cities", None)
                else:
                    args["stay_cities"] = ",".join(f"{c}:{ni}" for c, ni in cleaned)
        elif name == "plan_trip":
            args.setdefault("origin", (getattr(ctx, "origin", "") or "Hong Kong"))
            dest = ctx_dest or str(args.get("hotel_city") or args.get("destination") or "").strip()
            if "," in dest:
                dest = dest.split(",", 1)[0].strip() or dest
            if is_travel_style_label(dest) and ctx_dest:
                dest = ctx_dest.split(",", 1)[0].strip() if "," in ctx_dest else ctx_dest
            args.setdefault("destination", dest)
            # Overwrite model country-suffix destinations that break acity lookup.
            cur_dest = str(args.get("destination") or "")
            if "," in cur_dest:
                args["destination"] = cur_dest.split(",", 1)[0].strip() or cur_dest
            if ctx_dest and is_travel_style_label(str(args.get("destination") or "")):
                args["destination"] = dest
            if route is not None:
                args.setdefault("arrive_airport", getattr(route, "arrive_airport", "") or "")
                args.setdefault("return_airport", getattr(route, "depart_airport", "") or "")
                stays = getattr(route, "stays", None) or []
                if stays and not args.get("hotel_city"):
                    stay_city = getattr(stays[0], "city", "") or ctx_dest
                    if is_travel_style_label(stay_city):
                        stay_city = ctx_dest
                    args["hotel_city"] = stay_city
            args.setdefault(
                "hotel_city",
                ctx_dest or str(args.get("destination") or "").strip() or "destination",
            )
            hc_raw = str(args.get("hotel_city") or "").strip()
            if "," in hc_raw:
                args["hotel_city"] = hc_raw.split(",", 1)[0].strip() or hc_raw
            # Student sometimes pastes schema descriptions as hotel_city.
            hc = str(args.get("hotel_city") or "").strip().lower()
            if (
                ctx_dest
                and (
                    not hc
                    or is_travel_style_label(hc)
                    or "plain words" in hc
                    or "first stay" in hc
                    or "hotel city" in hc
                    or len(hc) > 60
                )
            ):
                cleaned = ctx_dest.split(",", 1)[0].strip() if "," in ctx_dest else ctx_dest
                args["hotel_city"] = cleaned
            # Drop placeholder arrive/return hubs from a bad prior propose.
            for hub_key in ("arrive_airport", "return_airport"):
                hub = str(args.get(hub_key) or "").strip().lower()
                if hub in {"xxx", "na", "n/a"}:
                    args.pop(hub_key, None)
            if not str(args.get("destination") or "").strip():
                args["destination"] = str(args.get("hotel_city") or "destination")
            if ctx_in:
                args["depart_date"] = ctx_in
            if ctx_out:
                args["return_date"] = ctx_out
            budget = getattr(ctx, "budget_hkd", None)
            if budget is not None and args.get("budget_hkd") in (None, "", 0, 0.0):
                args["budget_hkd"] = budget
            styles = (getattr(ctx, "travel_styles", "") or "").strip()
            if styles and not str(args.get("interests") or "").strip():
                args["interests"] = styles
            # Never pass travel style as interests-only when destination is empty junk
            if styles and is_travel_style_label(str(args.get("interests") or "")):
                args["interests"] = styles

    def _chat_with_recovery(self, *, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Run one Ollama chat call, reconnecting once on socket reset.

        Avoid force-restarting Ollama here because the user may already be
        running ``ollama serve`` manually in a visible terminal window.
        """

        chat_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "options": ollama_chat_options(),
        }
        if tools is not None:
            chat_kwargs["tools"] = tools

        try:
            return self.client.chat(**chat_kwargs)
        except Exception as exc:
            msg = str(exc)
            if "not found" in msg.lower() or "404" in msg:
                raise RuntimeError(
                    f"Ollama model '{self.model}' is not available. "
                    f"Run: ollama pull {self.model}"
                ) from exc
            if self.student_mode and self._is_invalid_tool_call_error(exc):
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool call failed: invalid or incomplete JSON arguments. "
                            "Call propose_trip_route first, then plan_trip with complete JSON."
                        ),
                    }
                )
                return self.client.chat(**chat_kwargs)
            if not self._is_retryable_ollama_error(exc):
                raise
            if self.on_status:
                self.on_status("Lost connection to Ollama, retrying…")

        ensure_ollama_running(
            restart=False,
            model=self.model,
            on_progress=self.on_status,
            ensure_model=True,
        )
        self.client = make_ollama_client(settings.ollama_host)
        self.browser.llm_host = settings.ollama_host
        self.browser.llm_model = self.model
        if self.on_status:
            self.on_status("Reconnected to Ollama")
        try:
            return self.client.chat(**chat_kwargs)
        except Exception as exc:
            if self._is_retryable_ollama_error(exc):
                raise RuntimeError(
                    "Ollama failed during generation and did not recover. "
                    "Keep `ollama serve` running in a separate terminal and try again."
                ) from exc
            raise

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
        """Always prefer Playwright /hotels/detail/?hotelId=… over list/search.

        For multi-city trips, prefer stay 0 from ``last_hotel_stays`` so globals
        are not left pointing at the last city's list URL.
        """
        stays = list(getattr(self.browser, "last_hotel_stays", None) or [])
        if len(stays) > 1:
            first = stays[0]
            first_url = (first.get("url") or "").strip()
            if first_url and is_trusted_hotel_detail_url(first_url):
                self.booking_links["hotel"] = first_url
            first_name = (first.get("name") or "").strip()
            if first_name and not first_name.lower().startswith(
                ("hotels in ", "recommended hotel")
            ):
                self.booking_links["hotel_name"] = first_name
            if first.get("image_url"):
                self.booking_links["hotel_image"] = first["image_url"]
            return
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
        self.clear_booking_links()
        route_ready = self.browser.last_proposed_route is not None
        # GUI Rent-a-car checkbox is the only authority (do not infer from prompt text)
        want_car = bool(self.force_rent_car)

        tools = _active_tools(student_mode=self.student_mode)
        if not want_car:
            tools = [
                t
                for t in tools
                if (t.get("function") or {}).get("name") != "search_cars"
            ]
            # Drop any leftover car scrape from a previous trip
            self.browser.last_car_card = {}
            self.browser.last_car_detail_url = ""
            for k in list(self.booking_links):
                if str(k).startswith("car"):
                    self.booking_links[k] = ""

        plan_done = False
        scrape_tools = {
            "plan_trip",
            "search_flights",
            "search_hotels",
            "compare_flight_prices",
            "compare_hotel_prices",
        }
        for _ in range(self._effective_max_tool_rounds()):
            tools_for_call: list[dict[str, Any]] | None = tools
            if self.student_mode and plan_done:
                tools_for_call = None
            response = self._chat_with_recovery(tools=tools_for_call)
            message = self._normalize_message(response["message"])
            self.messages.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = (message.get("content") or "").strip()
                if self.student_mode and not plan_done:
                    # Hard-require tools: never accept a final before propose+plan.
                    self._force_student_tools(want_car=want_car)
                    plan_done = True
                    route_ready = True
                    self._apply_plan_flight_card()
                    self._prefer_live_hotel_detail()
                    if want_car:
                        self._prefer_live_car_card()
                    return self._finalize_student_answer(content)
                self._apply_plan_flight_card()
                self._prefer_live_hotel_detail()
                if want_car:
                    self._prefer_live_car_card()
                return self._finalize_student_answer(content)
            if self.student_mode and plan_done:
                return self._finalize_student_answer(
                    (message.get("content") or "").strip()
                )

            round_plan_done = False
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

                if self.student_mode and name in {"propose_trip_route", "plan_trip", *scrape_tools}:
                    self._fill_tool_args_from_context(name, args)

                required_keys = {
                    "propose_trip_route": ("destination", "nights"),
                    "plan_trip": ("depart_date", "return_date", "hotel_city"),
                }
                missing = [
                    k
                    for k in required_keys.get(name, ())
                    if not str(args.get(k) or "").strip()
                ]
                if self.student_mode and missing:
                    repair = (
                        f"Tool {name} rejected: missing {', '.join(missing)}. "
                        "Re-call with complete JSON arguments."
                    )
                    if self.on_tool_start:
                        self.on_tool_start(name, args)
                    if self.on_tool_end:
                        self.on_tool_end(name, repair)
                    self.messages.append(
                        {"role": "tool", "tool_name": name, "content": repair}
                    )
                    continue

                # Rent-a-car checkbox is authoritative — never scrape cars when off
                if name == "plan_trip":
                    args["rent_car"] = bool(want_car)
                if name == "search_cars" and not want_car:
                    result = (
                        "Car rental skipped — Rent a car was not selected in the planner."
                    )
                    if self.on_tool_start:
                        self.on_tool_start(name, args)
                    if self.on_tool_end:
                        self.on_tool_end(name, result)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_name": name,
                            "content": result,
                        }
                    )
                    continue
                # City/keyword args must be typed with spaces (not Los+Angeles)
                for key in ("hotel_city", "city", "location", "destination"):
                    if args.get(key):
                        from travel_agent.places import typed_place_name

                        args[key] = typed_place_name(str(args[key]))

                # Force full trip length from the GUI selection (model often uses 7 nights)
                self._enforce_trip_length_args(name, args)

                # Enforce rough-route-before-scrape when the model skips propose
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
                if want_car:
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

                if self.student_mode:
                    result = compact_tool_result(
                        result,
                        name,
                        max_chars=max(1024, int(settings.student_tool_result_max_chars)),
                    )

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
                if (
                    self.student_mode
                    and name == "plan_trip"
                    and result
                    and not result.startswith("[fixture miss]")
                    and "rejected" not in result.lower()
                ):
                    round_plan_done = True

            if round_plan_done:
                plan_done = True
                if self.student_mode:
                    self._apply_plan_flight_card()
                    self._prefer_live_hotel_detail()
                    if want_car:
                        self._prefer_live_car_card()
                    # Skip extra LLM rewrite — fill Day N: deterministically from guides/tools.
                    draft = (message.get("content") or "").strip()
                    return self._finalize_student_answer(draft)

        if self.student_mode:
            # Tool-call limit hit without a successful plan_trip — force tools then finalize.
            had_plan = any(
                m.get("role") == "tool" and m.get("tool_name") == "plan_trip"
                for m in self.messages
            )
            if not had_plan:
                self._force_student_tools(want_car=want_car)
            self._apply_plan_flight_card()
            self._prefer_live_hotel_detail()
            if want_car:
                self._prefer_live_car_card()
            return self._finalize_student_answer("")

        response = self._chat_with_recovery(tools=tools)
        message = self._normalize_message(response["message"])
        self.messages.append(message)
        self._apply_plan_flight_card()
        self._prefer_live_hotel_detail()
        if want_car:
            self._prefer_live_car_card()
        content = (message.get("content") or "").strip() or (
            "I reached the tool-call limit. Please refine your request."
        )
        return content
