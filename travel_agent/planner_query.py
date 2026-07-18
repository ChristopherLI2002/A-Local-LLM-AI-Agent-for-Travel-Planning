"""Shared trip-plan prompt builder (plain text for desktop UI)."""

from __future__ import annotations


def build_plan_query(
    destination: str,
    depart_date: str,
    return_date: str | None,
    budget_hkd: float,
    origin: str = "HKG",
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
        "Do not plan with flights only when trains or airport transfers are selected — "
        "compare the relevant modes using plan_trip "
        f"(include_flights={str(include_flights).lower()}, "
        f"include_trains={str(include_trains).lower()}, "
        f"include_transfers={str(include_transfers).lower()}). {car_line} "
        "Use live Trip.com prices and stay within budget.\n\n"
        "Format your FINAL answer as clear plain text (no HTML, no code fences). "
        "Use short headings and bullet lists. Structure sections as: "
        "Overview, Transport comparison, Hotels, Car rental (only if needed), "
        "Day-by-day itinerary, Budget breakdown, Book on Trip.com, Next steps. "
        "Include live Trip.com search URLs from the tools as plain URLs for every "
        "mode searched. Never invent URLs — only use search URL values from tool output."
    )
