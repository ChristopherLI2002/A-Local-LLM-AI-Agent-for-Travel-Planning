"""Shared trip-plan prompt builder (Trip.Planner-style)."""

from __future__ import annotations

import re


TRAVEL_STYLES = (
    "First-time",
    "Culture",
    "Food",
    "Family",
    "Relaxed",
    "Adventure",
)

_TRAVEL_STYLE_SET = {s.lower() for s in TRAVEL_STYLES}

# LLM / student placeholders that must never appear on booking cards
PLACEHOLDER_CARD_TOKENS = (
    "see tool results",
    "see tool result",
    "see trip.com",
    "from tool results",
    "tool results",
    "unavailable",
    "n/a",
    "tbd",
    "unknown",
)


def is_travel_style_label(text: str) -> bool:
    """True when text is a wizard travel style (not a city / hotel area)."""
    raw = (text or "").strip().lower()
    if not raw:
        return False
    if raw in _TRAVEL_STYLE_SET:
        return True
    # "Stay · First-time (7 nights)" / multi-style "Food, Culture"
    parts = re.split(r"[,·|/]+", raw)
    cleaned = []
    for p in parts:
        p = re.sub(r"^\s*stay\s*\d*\s*", "", p, flags=re.I)
        p = re.sub(r"\(\s*\d+\s*nights?\s*\)", "", p, flags=re.I)
        p = p.strip(" -–—:")
        if p:
            cleaned.append(p)
    return bool(cleaned) and all(p in _TRAVEL_STYLE_SET for p in cleaned)


def is_placeholder_card_text(text: str) -> bool:
    """True for student/tool placeholder strings painted onto flight/hotel cards."""
    low = (text or "").strip().lower()
    if not low:
        return True
    if low in PLACEHOLDER_CARD_TOKENS:
        return True
    return any(tok in low for tok in PLACEHOLDER_CARD_TOKENS if len(tok) > 3)


def build_plan_query(
    destination: str,
    depart_date: str,
    return_date: str | None,
    budget_hkd: float,
    origin: str = "Hong Kong",
    rent_car: bool = False,
    include_flights: bool = True,
    include_trains: bool = True,
    include_transfers: bool = True,
    travel_styles: list[str] | None = None,
    nights: int | None = None,
) -> str:
    dates = (
        f"{depart_date} to {return_date}"
        if return_date
        else f"departing {depart_date}"
    )
    styles = [s for s in (travel_styles or []) if s]
    style_line = ", ".join(styles) if styles else "First-time"
    stay = f"{nights} nights" if nights else "full stay"

    modes: list[str] = []
    if include_flights:
        modes.append("flights")
    if include_trains:
        modes.append("trains")
    if include_transfers:
        modes.append("airport transfers")
    if rent_car:
        modes.append("rental car")
    if not modes:
        modes = ["flights", "trains", "airport transfers"]

    car_line = (
        "REQUIRED: call plan_trip with rent_car=true (also search_cars if needed)."
        if rent_car
        else (
            "Do NOT search for a rental car. Do not call search_cars. "
            "Pass rent_car=false to plan_trip."
        )
    )
    rent_arg = ", rent_car=true" if rent_car else ", rent_car=false"
    return_part = return_date or "(open return)"

    return (
        f"Create a Trip.Planner-style itinerary from {origin} to {destination}, "
        f"{dates} ({stay}), budget {budget_hkd:g} HKD. "
        f"Travel style(s): {style_line}. Modes: {', '.join(modes)}. {car_line}\n\n"
        "Workflow:\n"
        "1) FIRST call propose_trip_route with destination, nights, depart_date, "
        f"origin={origin!r}, interests={style_line!r}. "
        "This decides fly-into airport, fly-out airport, and the city stay order — "
        "do not search Trip.com yet.\n"
        "2) THEN call plan_trip with the same dates, hotel_city=first stay city "
        f"(plain words with spaces, never '+'), interests={style_line!r}, "
        "arrive_airport / return_airport from the rough route"
        f"{rent_arg}. Prefer Recommended hotel DETAIL links "
        "(/hotels/detail/?hotelId=...), not list/search pages.\n"
        "3) Use live hk.trip.com Canonical / Flight / Hotel / Car URLs from the tool — "
        "never invent www.trip.com links.\n\n"
        "Format the FINAL answer as plain text with EXACTLY these sections "
        "(headings must match):\n\n"
        "Recommended flight\n"
        "- Airline: <name if known>\n"
        "- From: <IATA> [terminal]   Depart time: HH:MM\n"
        "- To: <IATA> [terminal]     Arrive time: HH:MM\n"
        "- Duration: e.g. 4h 30m\n"
        "- Stops: Direct or N stop(s)\n"
        "- Baggage: e.g. Checked baggage 20 kg (if known)\n"
        "- Price: HK$… (or 'price unavailable on page')\n"
        "- Trip: Return or One-way\n"
        "- Link: <exact https://hk.trip.com/... URL>\n\n"
        "Recommended hotel\n"
        "- Hotel: <property name>\n"
        "- Stars: <1-5>\n"
        "- Score: <e.g. 9.0>  Reviews: <e.g. 261 reviews>\n"
        "- Location: <area • landmark>\n"
        "- Features: <e.g. Suite • breakfast • lounge>\n"
        "- Room: <e.g. Superior Twin>\n"
        "- Beds: <e.g. 2 single beds>\n"
        "- Nightly: HK$…\n"
        "- Total: HK$… (incl. taxes & fees) if known\n"
        "- Link: <exact https://hk.trip.com/... URL>\n\n"
        "Day-by-day itinerary\n"
        "Day 1:\n"
        "- 09:00 <metro/train route>\n"
        "- 10:00 <exact place>\n"
        "- 12:30 <exact restaurant>\n"
        "- 14:00 <exact place>\n"
        "- 19:00 <exact restaurant>\n"
        "Day 2:\n"
        "- ...\n"
        f"(REQUIRED: one 'Day N:' block for each day of the {stay} trip. "
        f"Pace and attractions MUST match travel style: {style_line}. "
        "Name real attractions and restaurants — no vague 'local dinner' lines. "
        "Do not replace this section with price tables.)\n\n"
        "Budget snapshot\n"
        "- Compare estimated flight+hotel total vs the user budget\n\n"
        "FORBIDDEN: inventing URLs/prices; one-night hotel when trip is a week; "
        "telling the user to compare flights/hotels themselves; "
        "omitting the Day-by-day itinerary section; vague day activities."
    )
