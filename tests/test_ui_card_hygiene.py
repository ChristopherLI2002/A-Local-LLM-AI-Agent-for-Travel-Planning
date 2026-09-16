"""Feedback loop: reject placeholder hotel/flight card junk + distinct thumbs."""

from __future__ import annotations

from travel_agent.attraction_images import (
    _loremflickr_image,
    images_for_timetable_offline,
)
from travel_agent.itinerary_parse import parse_hotel_offer, parse_itinerary
from travel_agent.planner_query import (
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
    assert len(imgs) == 4, imgs
    urls = list(imgs.values())
    assert len(set(urls)) == 4, urls
    assert all("picsum.photos/seed/" in u for u in urls), urls
    assert _loremflickr_image("a") != _loremflickr_image("b")

    print("PASS", urls[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
