"""Debug Trip.com flight result scraping."""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.browser_tools import TripBrowser
from travel_agent.config import settings

settings.headless = True
dep = (date.today() + timedelta(days=21)).isoformat()
ret = (date.today() + timedelta(days=28)).isoformat()

b = TripBrowser()
b.start()
page = b.page
params = {
    "dcity": "hkg",
    "acity": "par",
    "ddate": dep,
    "triptype": "rt",
    "rdate": ret,
    "class": "y",
    "quantity": 1,
    "searchboxarg": "t",
    "locale": "en_hk",
    "curr": "HKD",
}
url = f"https://hk.trip.com/flights/showfarefirst?{urlencode(params)}"
page.goto(url, wait_until="domcontentloaded", timeout=60000)
for _ in range(20):
    body = page.inner_text("body")
    if re.search(r"HK\s*\$\s*[0-9,]+", body) and re.search(r"\d{1,2}:\d{2}", body):
        break
    page.wait_for_timeout(1000)
page.mouse.wheel(0, 2000)
page.wait_for_timeout(2000)

body = page.inner_text("body")
print("BODY LEN", len(body))
print("TIMES", re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", body)[:20])
print("PRICES", re.findall(r"HK\s*\$\s*[0-9,]+", body)[:10])

m = re.search(r"HK\s*\$\s*[0-9,]+", body)
if m:
    chunk = body[max(0, m.start() - 500) : m.end() + 500]
    print("CONTEXT:\n", chunk)

alts = page.eval_on_selector_all(
    "img[alt]", "els => els.map(e => e.alt).filter(a => a && a.length > 1)"
)
print("IMG ALTS:", alts[:40])

card = b._scrape_top_flight_card(origin="HKG", destination="PAR", lowest=4998)
print("SCRAPED CARD:", card)

text = b.search_flights("hkg", "par", depart_date=dep, trip_type="roundtrip", return_date=ret)
m2 = re.search(r"Structured flight card:.*?(?=\n\n|\Z)", text, re.S)
print("\nSTRUCTURED:\n", m2.group(0) if m2 else "NONE")

out = ROOT / "scripts" / "debug_flight_body.txt"
out.write_text(body[:20000], encoding="utf-8")
print("Wrote", out)

b.close()
