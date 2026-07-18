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
        "Set rent_car=true."
        if rent_car
        else "Only include a rental car if clearly needed."
    )
    return_part = return_date or "(open return)"
    return (
        f"Plan a ROUND-TRIP trip from {origin} to {destination}, {dates}, "
        f"hotel stay from {depart_date} to {return_part}, "
        f"budget {budget_hkd:g} HKD. Modes: {', '.join(modes)}. {car_line}\n\n"
        "Call plan_trip once with matching depart_date/return_date and hotel_city="
        f"{destination}. "
        "plan_trip maps city names to airport codes automatically.\n\n"
        "Your final answer MUST start with:\n\n"
        "Recommended flight\n"
        "- Depart / return dates\n"
        "- Price (HKD) from tool output, or 'price unavailable on page'\n"
        "- Link: paste Canonical search URL or Flight search URL exactly "
        "(must be https://hk.trip.com/...)\n\n"
        "Recommended hotel\n"
        "- Check-in / check-out (full trip length, not 1 night)\n"
        "- Nightly and total if available\n"
        "- Link: paste Canonical search URL or Hotel search URL exactly "
        "(must be https://hk.trip.com/...)\n"
        "- Also paste any Hotel option link lines from tools\n\n"
        "Then add brief overview, budget vs user budget, and itinerary.\n\n"
        "FORBIDDEN:\n"
        "- Inventing www.trip.com/flights/search or /hotels/search URLs\n"
        "- Making up prices\n"
        "- Telling the user to compare flights/hotels themselves\n"
        "- One-night hotel checkout when the trip is a week\n"
    )
