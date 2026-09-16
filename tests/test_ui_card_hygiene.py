"""Feedback loop: reject placeholder hotel/flight card junk + distinct thumbs."""

from __future__ import annotations

from travel_agent.attraction_images import (
    _loremflickr_image,
    images_for_timetable_offline,
    is_generic_stock_image_url,
)
from travel_agent.itinerary_parse import (
    is_plausible_flight_clock_times,
    parse_hotel_offer,
    parse_itinerary,
)
from travel_agent.destination_guides import (
    DayIdea,
    activity_conflicts_destination,
    day_ideas_for,
    filter_attraction_plan_for_destination,
    format_day_body,
)
from travel_agent.planner_query import (
    is_junk_timetable_activity,
    is_placeholder_card_text,
    is_travel_style_label,
)


BAD_HOTEL_PLAN = """
Recommended hotel
- Hotel: see tool results
- Location: First-time
- Stars: 4
- Score: 8.4
- Nightly: See Trip.com

Day-by-day itinerary
Day 1: Arrival - Dotonbori
18:00 Land at airport (time from flight card)
19:15 Hotel check-in and drop bags
20:00 Takoyaki + okonomiyaki on Dotonbori
21:00 Rest at hotel after the flight
"""


def main() -> int:
    assert is_placeholder_card_text("see tool results")
    assert is_travel_style_label("First-time")
    assert is_travel_style_label("Stay · First-time (7 nights)")
    assert not is_travel_style_label("Osaka")

    assert not is_plausible_flight_clock_times(
        "18:00",
        "19:15",
        from_code="HKG",
        to_code="LGW",
        duration="",
    )
    assert is_plausible_flight_clock_times(
        "18:00",
        "06:30",
        from_code="HKG",
        to_code="LGW",
        duration="13h 30m",
    )

    offer = parse_hotel_offer(BAD_HOTEL_PLAN)
    assert not offer.name or "see tool" not in offer.name.lower(), offer.name

    parsed = parse_itinerary(BAD_HOTEL_PLAN)
    assert parsed.hotel_offer is not None
    assert "see tool" not in (parsed.hotel_offer.name or "").lower(), parsed.hotel_offer.name

    body = (
        "18:00 Land at airport\n"
        "19:15 Hotel check-in\n"
        "20:00 Takoyaki on Dotonbori\n"
        "21:00 Rest at hotel\n"
    )
    imgs = images_for_timetable_offline(body, "Osaka")
    assert len(imgs) == 0, imgs
    assert is_generic_stock_image_url(_loremflickr_image("a"))
    assert _loremflickr_image("a") != _loremflickr_image("b")

    from travel_agent.attraction_images import resolve_timetable_image_url

    wiki = resolve_timetable_image_url(
        "British Museum (Great Court) — pick 2–3 rooms", "London"
    )
    assert wiki and "wikimedia" in wiki.lower(), wiki

    assert is_junk_timetable_activity(
        "Hong Kong Special Administrative Region (HK$12,000 Total Budget)"
    )
    assert is_junk_timetable_activity("Laver Cup 2026 (Open 2026.09.25-2026.09.27)")

    assert activity_conflicts_destination("Hong Kong City Hall, N/A", "London")
    assert not activity_conflicts_destination("Westminster Abbey", "London")
    hk_plan = [
        {
            "title": "Arrival · London",
            "go": "Hong Kong City Hall, N/A",
            "also": "Victoria Peak",
            "lunch": "Lunch",
            "dinner": "Dinner",
            "route": "MTR",
        }
    ]
    assert filter_attraction_plan_for_destination(hk_plan, "London") == []
    london_ideas = day_ideas_for(
        "London",
        3,
        attraction_plan=hk_plan,
    )
    assert any("Westminster" in (d.go + d.also) for d in london_ideas), london_ideas

    idea = DayIdea(
        title="Arrival · Hong Kong",
        go="Land side",
        also="Victoria Peak",
        lunch="Dim sum",
        dinner="Temple Street",
        route="Airport Express / MTR",
    )
    body = format_day_body(idea, arrive_time="09:10")
    assert "09:10" in body
    assert "Hotel check-in" in body
    # After 60 min airport transfer + 75 min immigration floor → check-in ≥ 10:25
    assert "10:25" in body or "10:10" in body
    checkin_line = next(l for l in body.splitlines() if "Hotel check-in" in l)
    checkin_time = checkin_line.split()[0]
    check_h, check_m = map(int, checkin_time.split(":"))
    assert check_h * 60 + check_m >= 10 * 60 + 25, body

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
