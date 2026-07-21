"""Verify structured tool cards win over bad LLM prose."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.itinerary_parse import parse_flight_offer, parse_hotel_offer, parse_itinerary
from travel_agent.trip_urls import extract_booking_urls

BAD_PLAN = """
Recommended flight
- Airline: Trip.com fare
- Duration: 7 nights (July 21 - July 28)
- Price: See Trip.com

Recommended hotel
- Hotel: Sample Hotel Options: Rates range from HK$900 to HK$4,900 per night.
- Location: Paris

Day 1: Arrive
"""

TOOL_TEXT = """
Canonical search URL: https://hk.trip.com/flights/showfarefirst?dcity=hkg&acity=par
Structured flight card:
- Airline: Air France
- Depart: 09:15
- Arrive: 16:40
- From: HKG
- To: CDG
- Duration: 13h 25m
- Stops: Direct
- Price: HK$4,280

RECOMMENDED HOTEL
- Lowest nightly: HK$4,800
- Est. stay total: HK$33,600
Structured hotel card:
- Hotel: Le Meurice Paris
- Stars: 5
- Score: 9.3
- Location: Paris
- Nightly: HK$4,800
- Reviews: 45 reviews
Recommended hotel name: Le Meurice Paris
Recommended hotel detail link: https://hk.trip.com/hotels/detail/?hotelId=2151304&cityId=192&checkIn=2026-07-21&checkOut=2026-07-28
"""


def main() -> int:
    found = extract_booking_urls(TOOL_TEXT)
    assert found.get("flight_airline") == "Air France", found
    junk = extract_booking_urls(
        "Structured flight card:\n- Airline: qrcode\n- Price: HK$4,990\n"
    )
    assert "flight_airline" not in junk, junk
    assert found.get("flight_depart") == "09:15", found
    assert found.get("hotel_name") == "Le Meurice Paris", found
    assert "4,800" in found.get("hotel_price", ""), found
    assert found.get("hotel"), found

    parsed = parse_itinerary(BAD_PLAN)
    fo = parsed.flight_offer
    ho = parsed.hotel_offer
    assert fo is not None and ho is not None
    # Duration must not keep hotel nights text
    assert "night" not in (fo.duration or "").lower(), fo.duration
    # Sample hotel name should be rejected → default placeholder
    assert "sample" not in (ho.name or "").lower(), ho.name

    # Apply tool fields the way GUI enrichment does
    fo.airline = found["flight_airline"]
    fo.depart_time = found["flight_depart"]
    fo.arrive_time = found["flight_arrive"]
    fo.depart_airport = found["flight_from"]
    fo.arrive_airport = found["flight_to"]
    fo.duration = found["flight_duration"]
    fo.price_label = found["flight_price"]
    ho.name = found["hotel_name"]
    ho.price_label = found["hotel_price"]
    ho.score = found["hotel_score"]

    print("flight", fo.airline, fo.depart_time, fo.arrive_time, fo.duration, fo.price_label)
    print("hotel", ho.name, ho.price_label, ho.score)
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
