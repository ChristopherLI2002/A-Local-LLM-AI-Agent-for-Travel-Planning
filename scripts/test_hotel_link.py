"""Fix-test loop for hotel booking links (max 20 epochs)."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("HEADLESS", "true")

from travel_agent.browser_tools import TripBrowser
from travel_agent.itinerary_parse import parse_itinerary
from travel_agent.trip_urls import (
    extract_booking_urls,
    fetch_hotel_detail_link,
    is_trusted_hotel_detail_url,
    resolve_booking_url,
)

FAKE_PLAN = """
Recommended hotel
- Hotel: Hôtel de la Cité des Halles
- Location: Place Vendôme
- Nightly: HK$4,800
- Link: https://hk.trip.com/hotels/detail/?hotelId=123456

### Day-by-day itinerary
Day 1: Arrive in Paris
"""


def epoch(n: int) -> bool:
    depart = (date.today() + timedelta(days=1)).isoformat()
    checkout = (date.today() + timedelta(days=8)).isoformat()

    parsed = parse_itinerary(FAKE_PLAN)
    name = parsed.hotel_offer.name if parsed.hotel_offer else ""
    parsed_url = parsed.hotel_offer.url if parsed.hotel_offer else ""

    ok_name = "day-by-day" not in name.lower() and "###" not in name
    ok_fake_rejected = not is_trusted_hotel_detail_url(parsed_url)

    browser = TripBrowser()
    browser.start()
    try:
        live = fetch_hotel_detail_link(
            browser,
            city="Paris",
            checkin=depart,
            checkout=checkout,
        )
        tool_url = live.get("url", "")
        tool_name = live.get("name", "")
    finally:
        browser.close()

    hotel_url = resolve_booking_url(
        "hotel",
        tool_url=tool_url,
        built_url="",
        parsed_url=parsed_url,
    )
    ok_url = is_trusted_hotel_detail_url(hotel_url)
    ok_has_dates = "checkIn=" in hotel_url or "checkin=" in hotel_url.lower()

    print(f"epoch {n:2d}: name={name[:40]!r} url_ok={ok_url} fake_rejected={ok_fake_rejected} name_ok={ok_name} dates={ok_has_dates}")
    if ok_url:
        print(f"         url={hotel_url[:120]}...")
        print(f"         scraped_name={tool_name!r}")

    return ok_url and ok_fake_rejected and ok_name and ok_has_dates


def main() -> int:
    max_epochs = 20
    for n in range(1, max_epochs + 1):
        try:
            if epoch(n):
                print(f"PASS at epoch {n}")
                return 0
        except Exception as exc:
            print(f"epoch {n:2d}: ERROR {exc}")
    print(f"FAIL after {max_epochs} epochs")
    return 1


if __name__ == "__main__":
    sys.exit(main())
