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
        "You MUST actually compare and pick flights and hotels — do not leave that as "
        "homework for the user.\n\n"
        "Required tool workflow:\n"
        "1) Call plan_trip "
        f"(include_flights={str(include_flights).lower()}, "
        f"include_trains={str(include_trains).lower()}, "
        f"include_transfers={str(include_transfers).lower()}, "
        f"hotel_city resolved from destination). {car_line}\n"
        "2) plan_trip already runs live flight-date and hotel-date comparisons on Trip.com.\n"
        "3) If plan_trip flight/hotel rankings look thin, ALSO call "
        "compare_flight_prices and/or compare_hotel_prices yourself, then merge results.\n\n"
        "Use live Trip.com prices and stay within budget.\n\n"
        "Format your FINAL answer as clear plain text (no HTML, no code fences). "
        "Use short headings and bullet lists. Structure sections as:\n"
        "- Overview\n"
        "- Recommended flight (date, lowest HKD, why chosen, Trip.com URL)\n"
        "- Recommended hotel (rate band / option, nights total, Trip.com URL)\n"
        "- Transport comparison (flights vs trains vs transfers as applicable)\n"
        "- Car rental (only if needed)\n"
        "- Day-by-day itinerary\n"
        "- Budget breakdown vs the user's budget\n"
        "- Book on Trip.com (real search URLs only)\n\n"
        "Do NOT write a 'Next steps' list that tells the user to compare flights or hotels. "
        "You already did that. End with booking URLs and a short confirmation of the picks. "
        "Never invent URLs or prices — only use tool output."
    )
