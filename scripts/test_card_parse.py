"""Quick check that left-column flight/hotel cards parse with details."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.itinerary_parse import parse_itinerary

SAMPLE = """
### Recommended flight
- Airline: Air France
- From: HKG to CDG
- Depart: 09:15 Arrive: 16:40
- Duration: 13h 25m
- Price: HK$4,280
- Link: https://hk.trip.com/flights/showfarefirst?dcity=hkg&acity=par

### Recommended hotel
- Hotel: Le Meurice Paris
- Location: Place Vendome
- Stars: 5
- Score: 9.3
- Room: Superior Twin
- Nightly: HK$4,800
- Total: HK$33,600
- Link: https://hk.trip.com/hotels/detail/?hotelId=2151304&cityId=192

### Day-by-day itinerary
Day 1: Arrive in Paris
"""


def main() -> int:
    p = parse_itinerary(SAMPLE)
    fo, ho = p.flight_offer, p.hotel_offer
    assert fo is not None and ho is not None
    print(
        "flight",
        fo.airline,
        fo.depart_time,
        fo.arrive_time,
        fo.depart_airport,
        fo.arrive_airport,
        fo.price_label,
    )
    print(
        "hotel",
        ho.name,
        ho.location,
        ho.price_label,
        ho.total_label,
        ho.room_type,
        ho.stars,
        ho.score,
    )
    ok = (
        fo.airline == "Air France"
        and fo.depart_time == "09:15"
        and fo.arrive_time == "16:40"
        and fo.arrive_airport == "CDG"
        and "4,280" in fo.price_label
        and ho.name == "Le Meurice Paris"
        and "4,800" in ho.price_label
        and ho.room_type == "Superior Twin"
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
