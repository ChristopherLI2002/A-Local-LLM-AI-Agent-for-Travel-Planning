"""Inspect hotel detail page for photo URLs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.browser_tools import TripBrowser
from travel_agent.config import settings
from travel_agent.trip_urls import fetch_hotel_detail_link

settings.headless = True
b = TripBrowser()
b.start()
live = fetch_hotel_detail_link(
    b, city="Tokyo", checkin="2026-07-23", checkout="2026-07-30"
)
print("hotel", live.get("name"), live.get("url", "")[:120])
url = live.get("url", "")
page = b.page
if url:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

imgs = page.evaluate(
    """() => Array.from(document.querySelectorAll('img')).map(e => ({
      src: e.currentSrc || e.src || '',
      alt: (e.alt || '').slice(0, 80),
      w: e.naturalWidth || e.width || 0,
      h: e.naturalHeight || e.height || 0
    })).filter(x => x.src && !x.src.startsWith('data:'))"""
)
print("imgs", len(imgs))
for im in sorted(imgs, key=lambda x: -(x["w"] * x["h"]))[:20]:
    print(f"{im['w']}x{im['h']} | {im['alt'][:40]!r} | {im['src'][:150]}")

og = page.locator("meta[property='og:image']")
if og.count():
    print("OG:", og.first.get_attribute("content"))

b.close()
