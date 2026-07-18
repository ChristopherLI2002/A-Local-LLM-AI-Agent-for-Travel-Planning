"""Shared trip-plan prompt builder (plain text for desktop UI)."""

from __future__ import annotations


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
) -> str:
    dates = (
        f"{depart_date} to {return_date}"
        if return_date
        else f"departing {depart_date}"
    )
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
        "Set rent_car=true and include the car rental search URL."
        if rent_car
        else "Only include a rental car if clearly needed."
    )
    return (
        f"Plan a trip from {origin} to {destination}, {dates}, "
        f"budget {budget_hkd:g} HKD. Consider these transport modes: {', '.join(modes)}. "
        "You MUST compare and recommend a specific flight and hotel with live Trip.com links.\n\n"
        "Required tool workflow:\n"
        "1) Call plan_trip "
        f"(include_flights={str(include_flights).lower()}, "
        f"include_trains={str(include_trains).lower()}, "
        f"include_transfers={str(include_transfers).lower()}, "
        f"hotel_city resolved from destination). {car_line}\n"
        "2) Copy the RECOMMENDED FLIGHT and RECOMMENDED HOTEL blocks from tool output "
        "into your final answer, including every booking URL exactly as returned.\n\n"
        "Format your FINAL answer as clear plain text (no HTML, no code fences).\n"
        "Start with these two sections first:\n"
        "1) Recommended flight — date, price (HKD), reason, and the full Trip.com link\n"
        "2) Recommended hotel — check-in, nightly/total (HKD), option text, and the full "
        "Trip.com hotel search/detail link(s)\n"
        "Then: Overview, transport comparison, itinerary, budget vs user budget.\n\n"
        "Rules:\n"
        "- Always include the recommended flight link and recommended hotel link.\n"
        "- Never invent URLs or prices — only paste tool URLs.\n"
        "- Do NOT tell the user to compare flights/hotels themselves.\n"
        "- Prefer the cheapest ranked options from plan_trip."
    )
