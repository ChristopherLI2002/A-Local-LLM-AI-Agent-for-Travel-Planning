"""Compact system prompt and slim tool schemas for the student (context distillation).

Teacher uses the full SYSTEM_PROMPT + TOOL_DEFINITIONS (~3193 tokens).
Student is trained to produce the same outputs from this compact form (~850 tokens).
"""

from __future__ import annotations

import re
from typing import Any

from travel_agent.browser_tools import TOOL_DEFINITIONS

COMPACT_SYSTEM_PROMPT = """You are a Trip.Planner-style concierge for hk.trip.com (HKD).

Workflow (STRICT):
1) Call propose_trip_route first (airports + city stay order). Do not scrape yet.
2) Call plan_trip with arrive/return airports from the route (rent_car if asked).
3) After tools return, write the COMPLETE final plan in ONE assistant message.
   Do not ask the user questions. Do not echo the workflow. Do not stop early.

Final answer MUST use these exact headings (in order):
Recommended flight
Recommended hotel
Day-by-day itinerary
Budget snapshot

Under Day-by-day itinerary, emit exactly one line per night as:
Day 1: ...
Day 2: ...
Day N: ...
(N = number of nights). Each day needs named places from tool results.

Rules: only https://hk.trip.com/... links from tools; hotel DETAIL URLs
(/hotels/detail/?hotelId=…); named places only (no "Attractions & Tours" or
"local dinner"); city names with spaces; match travel style pace.
"""


def _slim_parameters(params: dict[str, Any] | None) -> dict[str, Any]:
    """Keep types/required; drop verbose property descriptions."""
    if not params:
        return {"type": "object", "properties": {}}
    out = {"type": params.get("type") or "object", "properties": {}}
    props = params.get("properties") or {}
    for name, schema in props.items():
        if not isinstance(schema, dict):
            out["properties"][name] = schema
            continue
        slim: dict[str, Any] = {}
        if "type" in schema:
            slim["type"] = schema["type"]
        if "enum" in schema:
            slim["enum"] = schema["enum"]
        # No property descriptions — names are enough for a distilled student
        out["properties"][name] = slim or {"type": "string"}
    if "required" in params:
        out["required"] = params["required"]
    return out


def _slim_tool(defn: dict[str, Any]) -> dict[str, Any]:
    """Keep name + slim parameters; shorten description to one short line."""
    out = dict(defn)
    fn = dict(out.get("function") or {})
    name = fn.get("name") or ""
    desc = (fn.get("description") or "").strip()
    short = desc.split(". ")[0].strip()
    if len(short) > 100:
        short = short[:97] + "…"
    if short and not short.endswith("."):
        short += "."
    fn["description"] = short or name
    fn["parameters"] = _slim_parameters(fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {})
    out["function"] = fn
    out["type"] = out.get("type") or "function"
    return out


# Student tools: same schemas, short descriptions (params unchanged for tool calling)
SLIM_TOOL_DEFINITIONS: list[dict[str, Any]] = [_slim_tool(t) for t in TOOL_DEFINITIONS]


_SECTION_MARKERS = (
    "ROUGH TRIP ROUTE",
    "RECOMMENDED FLIGHT",
    "RECOMMENDED HOTEL",
    "YOUR RECOMMENDED BOOKINGS",
    "TRIP PLAN",
    "Attraction",
    "ATTRACTION",
    "Budget",
)


def compact_tool_result(
    text: str,
    tool_name: str = "",
    *,
    max_chars: int = 3500,
) -> str:
    """Shrink large scrape payloads so student models keep room for the final plan."""
    if not text or len(text) <= max_chars:
        return text

    lines = text.splitlines()
    kept: list[str] = []
    option_count = 0
    for line in lines:
        stripped = line.strip()
        if any(marker in line for marker in _SECTION_MARKERS):
            kept.append(line)
            option_count = 0
            continue
        if re.match(r"^\d+\.\s", stripped):
            option_count += 1
            if option_count <= 2:
                kept.append(line)
            continue
        if stripped.startswith("-") or stripped.startswith("http") or stripped.startswith("Day "):
            kept.append(line)
            continue
        if len(kept) < 100:
            kept.append(line)

    compact = "\n".join(kept).strip()
    if len(compact) > max_chars:
        compact = compact[:max_chars].rstrip() + "\n\n[...truncated for student context...]"
    elif not compact:
        compact = text[:max_chars].rstrip() + "\n\n[...truncated for student context...]"
    return compact


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def footprint() -> dict[str, int]:
    import json

    sp = estimate_tokens(COMPACT_SYSTEM_PROMPT)
    td = estimate_tokens(json.dumps(SLIM_TOOL_DEFINITIONS))
    return {
        "compact_system_tokens": sp,
        "slim_tools_tokens": td,
        "total_overhead_tokens": sp + td,
        "n_tools": len(SLIM_TOOL_DEFINITIONS),
    }
