"""Place / IATA resolution — country suffixes must not become acity=xxx."""

from __future__ import annotations

from travel_agent.places import to_flight_code, to_hotel_city, to_hotel_city_id
from travel_agent.regions import propose_trip_route


def test_flight_code_strips_country_suffix() -> None:
    assert to_flight_code("Tokyo, Japan") == "tyo"
    assert to_flight_code("Seoul, Korea") == "sel"
    assert to_flight_code("Paris, France") == "par"
    assert to_flight_code("Singapore") == "sin"
    assert to_flight_code("Tokyo") == "tyo"


def test_hotel_city_strips_country_suffix() -> None:
    assert to_hotel_city("Tokyo, Japan") == "Tokyo"
    assert to_hotel_city("Seoul, Korea") == "Seoul"
    assert to_hotel_city_id("Tokyo, Japan") == "228"
    assert to_hotel_city_id("Seoul, Korea") == "274"


def test_propose_route_never_uses_xxx_for_known_cities() -> None:
    for dest in ("Tokyo, Japan", "Seoul, Korea", "Tokyo", "Singapore"):
        route = propose_trip_route(dest, 4, depart_date="2026-10-12")
        assert route.arrive_airport.lower() not in {"", "xxx"}, dest
        assert route.depart_airport.lower() not in {"", "xxx"}, dest
        assert len(route.arrive_airport) == 3, dest


def test_stay_cities_tokyo_japan_is_one_stay() -> None:
    from travel_agent.regions import parse_stay_cities_arg, propose_trip_route

    assert parse_stay_cities_arg("Tokyo, Japan", total_nights=7) == [("Tokyo", 7)]
    assert parse_stay_cities_arg("Tokyo:4,Japan:3", total_nights=7) == [("Tokyo", 7)]
    multi = parse_stay_cities_arg("San Francisco:4,Los Angeles:3", total_nights=7)
    assert multi == [("San Francisco", 4), ("Los Angeles", 3)]

    route = propose_trip_route(
        "Tokyo",
        7,
        depart_date="2026-09-17",
        stay_cities="Tokyo, Japan",
    )
    assert len(route.stays) == 1
    assert route.stays[0].city == "Tokyo"
    assert route.stays[0].nights == 7
