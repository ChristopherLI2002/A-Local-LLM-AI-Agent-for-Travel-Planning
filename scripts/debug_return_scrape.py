"""Debug: click outbound Select and scrape return flight list."""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.browser_tools import TripBrowser, parse_trip_com_flight_rows
from travel_agent.config import settings

settings.headless = True
dep = (date.today() + timedelta(days=21)).isoformat()
ret = (date.today() + timedelta(days=28)).isoformat()

b = TripBrowser()
b.start()
text = b.search_flights("hkg", "par", depart_date=dep, trip_type="roundtrip", return_date=ret)
page = b.page

# Try clicking first Select button in results
btns = page.get_by_role("button", name=re.compile(r"^Select$", re.I))
print("Select buttons:", btns.count())
labels = page.get_by_text(re.compile(r"^Select$", re.I))
print("Select text nodes:", labels.count())
if labels.count():
    labels.first.click(timeout=5000)
    page.wait_for_timeout(4000)
    body = page.inner_text("body")
    print("URL after click:", page.url)
    print("Return markers:", re.findall(r"(?i).{0,40}(return|inbound|departures to).{0,40}", body)[:15])
    rows = parse_trip_com_flight_rows(body, origin="PAR", destination="HKG")
    print("Parsed rows after click:", len(rows))
    for r in rows[:3]:
        print(r)
    out = ROOT / "scripts" / "debug_return_body.txt"
    out.write_text(body[:25000], encoding="utf-8")
    print("Wrote", out)

b.close()
