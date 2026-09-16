"""Playwright browser tools for searching Trip.com Hong Kong."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from travel_agent.config import settings
from travel_agent.airline_names import expand_airline_code, airline_logo_url, is_plausible_airline_name
from travel_agent.places import (
    to_flight_code,
    to_hotel_city,
    to_hotel_city_id,
    typed_place_name,
)
from travel_agent.pricing import (
    format_comparison_table,
    nearby_dates,
    nights_between,
    parse_prices,
    pick_cheapest_from_comparison,
    summarize_prices,
)
from travel_agent.llm_select import (
    SelectionContext,
    _attraction_base_key,
    arrange_attraction_route,
    enrich_car_candidates_from_page,
    enrich_hotel_candidates_from_page,
    select_car_candidate,
    select_flight_row,
    select_hotel_candidate,
)
from travel_agent.trip_urls import (
    build_flight_search_url,
    ensure_locale_curr,
    normalize_trip_url,
    extract_booking_urls,
)

TRIP_HOME = f"{settings.trip_base_url}/?locale={settings.trip_locale}&curr={settings.trip_currency}"


def _prefer_hotel_photo_url(src: str) -> str:
    """Normalize TripCDN thumbs toward a larger JPEG cover when possible."""
    out = (src or "").split("?")[0].strip()
    if not out:
        return ""
    # List cards often use 600x600 webp thumbs — prefer the 960x660 JPEG sibling
    out = re.sub(
        r"_R_600_600_R5_D\.jpg_\.webp$",
        "_R_960_660_R5_D.jpg",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"_R_600_600_R5_D\.webp$",
        "_R_960_660_R5_D.jpg",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"_R_\d+_\d+_R5_D\.jpg_\.webp$",
        "_R_960_660_R5_D.jpg",
        out,
        flags=re.I,
    )
    return out


def _http_fetch_hotel_detail_meta(detail_url: str) -> dict[str, str]:
    """Parse name / score / cover from Trip.com detail HTML (no Playwright).

    Playwright navigations to `/hotels/detail` redirect to sign-in, but a normal
    HTTP GET still returns the SSR `hotelDetailResponse` payload used by the
    public detail page (e.g. THE KNOT TOKYO Shinjuku).
    """
    out: dict[str, str] = {}
    url = (detail_url or "").strip()
    if not url or "trip.com" not in url.lower() or "hotelid=" not in url.lower():
        return out
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-HK,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return out
    if not html or len(html) < 2000:
        return out
    flat = html.replace('\\"', '"')

    name = ""
    m = re.search(
        r'"nameInfo"\s*:\s*\{[^}]{0,400}?"name"\s*:\s*"([^"]{3,120})"',
        flat,
    )
    if m:
        name = m.group(1)
    if not name:
        m = re.search(r'"hotelNames"\s*:\s*\[\s*"([^"]{3,120})"', flat)
        if m:
            name = m.group(1)
    if not name:
        m = re.search(
            r'"seoTdk"\s*:\s*\{[^}]{0,200}?"title"\s*:\s*"([^"]{3,160})"',
            flat,
        )
        if m:
            name = m.group(1)
    if name:
        cleaned = clean_hotel_display_name(name)
        if cleaned and 3 < len(cleaned) < 120:
            out["name"] = cleaned

    m = re.search(
        r'"hotelComment"\s*:\s*\{\s*"comment"\s*:\s*\{([^}]{0,500})\}',
        flat,
    )
    if m:
        block = m.group(1)
        sm = re.search(r'"score"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', block)
        if sm:
            out["score"] = sm.group(1)
            try:
                val = float(out["score"])
                desc = re.search(r'"scoreDescription"\s*:\s*"([^"]+)"', block)
                out["score_label"] = (
                    (desc.group(1).strip() if desc else "")
                    or (
                        "Great"
                        if val >= 9
                        else "Very Good"
                        if val >= 8
                        else "Good"
                    )
                )
            except ValueError:
                pass
        rm = re.search(r'"totalComment"\s*:\s*(\d+)', block)
        if rm:
            out["reviews"] = f"{int(rm.group(1)):,} reviews"

    m = re.search(r'"starInfo"\s*:\s*\{[^}]{0,120}?"level"\s*:\s*(\d+)', flat)
    if m and 1 <= int(m.group(1)) <= 5:
        out["stars"] = m.group(1)

    m = re.search(
        r'"imgUrl"\s*:\s*"(https://[^"]*(?:tripcdn|ak-d\.tripcdn)[^"]+)"',
        flat,
    )
    if m:
        out["image_url"] = _prefer_hotel_photo_url(m.group(1))

    loc = ""
    m = re.search(r'"cityName"\s*:\s*"([^"]{2,60})"', flat)
    if m:
        loc = m.group(1)
    m = re.search(
        r'"(?:fullAddress|addressDetail|address)"\s*:\s*"([^"]{5,120})"',
        flat,
    )
    if m:
        loc = m.group(1)
    if loc:
        out["location"] = loc[:80]

    return out


def _http_fetch_attraction_detail_meta(detail_url: str) -> dict[str, str]:
    """Parse name / photo / address / hours / visit time from Trip.com attraction HTML."""
    out: dict[str, str] = {}
    url = (detail_url or "").strip()
    if not url or "trip.com" not in url.lower():
        return out
    if "/travel-guide/attraction/" not in url.lower() and "/things-to-do/" not in url.lower():
        return out
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-HK,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return out
    if not html or len(html) < 1500:
        return out
    flat = html.replace('\\"', '"')

    for pat in (
        r'"poiName"\s*:\s*"([^"]{2,120})"',
        r'"displayName"\s*:\s*"([^"]{2,120})"',
        r'"ename"\s*:\s*"([^"]{2,120})"',
    ):
        m = re.search(pat, flat)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            if name and len(name) > 2 and "attraction" not in name.lower():
                out["name"] = name
                break

    m = re.search(
        r'"coverImageUrl"\s*:\s*"(https://[^"]*(?:tripcdn|ak-d\.tripcdn)[^"]+)"',
        flat,
    )
    if not m:
        m = re.search(
            r'"coverImage"\s*:\s*"(https://[^"]*(?:tripcdn|ak-d\.tripcdn)[^"]+)"',
            flat,
        )
    if m:
        out["image_url"] = m.group(1).replace("\\u002F", "/")

    m = re.search(r'"address"\s*:\s*"([^"]{8,200})"', flat)
    if m and "@type" not in m.group(1):
        out["address"] = re.sub(r"\s+", " ", m.group(1)).strip()

    m = re.search(r'"openTimeDesc"\s*:\s*"([^"]{3,80})"', flat)
    if m:
        hours = re.sub(r"\s+", " ", m.group(1)).strip()
        hours = re.sub(r"(?i)^open:\s*", "", hours)
        hours = hours.replace("–", "-").replace("—", "-")
        out["open_hours"] = hours

    m = re.search(r'"playSpendTime"\s*:\s*"([^"]{2,40})"', flat)
    if m:
        spend = re.sub(r"\s+", " ", m.group(1)).strip().replace("–", "-").replace("—", "-")
        out["visit_time"] = spend

    return out


_CN_HOTEL_MARKERS = (
    "lishui",
    "leshan",
    "longyan",
    "nanping",
    "changzhou",
    "high speed railway",
    "railway station shop",
    "高铁",
    "火车站",
    "beijing",
    "shanghai",
    "guangzhou",
    "shenzhen",
    "hangzhou",
    "chengdu",
    "wuhan",
    "nanjing",
    "suzhou",
    "xian",
    "xi'an",
)


def _hotel_name_plausible_for_city(name: str, city: str) -> bool:
    """Reject obviously wrong-region hotel titles (e.g. China listings for SF)."""
    low = (name or "").lower()
    city_l = (city or "").lower()
    if not low or "sample" in low or "rates range" in low:
        return False
    # US / Western city stays should not show mainland-China rail-station hotels
    western = any(
        x in city_l
        for x in (
            "san francisco",
            "los angeles",
            "san diego",
            "new york",
            "miami",
            "orlando",
            "tampa",
            "florida",
            "london",
            "paris",
            "tokyo",
            "seoul",
            "singapore",
            "sydney",
            "california",
            "rome",
        )
    )
    if western and any(m in low for m in _CN_HOTEL_MARKERS):
        return False
    if re.search(r"[\u4e00-\u9fff]", name or "") and western:
        return False
    return True


def clean_hotel_display_name(name: str, city: str = "") -> str:
    """Keep the Trip.com property title; only strip SEO page junk.

    Official page heading (keep as-is):
      "InterContinental Hotels SAN DIEGO by IHG"
    og:title / browser tab often appends junk (strip this only):
      "InterContinental Hotels SAN DIEGO by IHG: 2026 Deals & Reviews"
    """
    del city  # kept for call-site compatibility; do not rewrite the official name
    raw = (name or "").strip()
    if not raw:
        return ""
    # First line only (some scrapes include address under the title)
    raw = raw.split("\n", 1)[0].strip()
    # Drop pipe/em-dash site suffixes: "Name | Trip.com"
    raw = re.split(r"\s*[|\u2013\u2014]\s*", raw)[0].strip()
    # Drop ": 2026 Deals & Reviews" / ": Deals & Reviews" SEO tails
    raw = re.sub(
        r"\s*:\s*(?:\d{4}\s+)?Deals?\s*&?\s*Reviews?.*$",
        "",
        raw,
        flags=re.I,
    )
    raw = re.sub(
        r"\s+[-–—]\s*(?:Trip\.com|Booking\.com|Hotels\.com|Expedia).*$",
        "",
        raw,
        flags=re.I,
    )
    raw = re.sub(r"\s*\(\d{4}\)\s*$", "", raw)
    raw = re.sub(r"\s{2,}", " ", raw).strip(" :-–—")
    if len(raw) > 100:
        raw = raw[:97].rstrip(" -") + "…"
    return raw


def _city_hotel_fallback_image(city: str, *, hotel_name: str = "") -> str:
    """Stable city hotel photo when Trip.com cover scrape fails."""
    from travel_agent.attraction_images import lookup_image, _loremflickr_image

    key = (city or "hotel").strip() or "hotel"
    fb = _CITY_STAY_FALLBACKS.get(key.lower()) or {}
    queries = [
        hotel_name,
        fb.get("name", ""),
        fb.get("image_query", ""),
        f"{key} hotel exterior",
    ]
    for query in queries:
        q = (query or "").strip()
        if not q:
            continue
        try:
            url = lookup_image(q, city=key)
        except Exception:
            url = ""
        if not url or not url.startswith("http"):
            continue
        low = url.lower()
        if any(
            bad in low
            for bad in ("capsule_hotel", "capsule-hotel", "pod_hotel", "hostel_dorm")
        ):
            continue
        return url
    return _loremflickr_image(f"hotel,exterior,{key}", width=640, height=360)


# Well-known city hotels used when Trip.com list scrape returns nothing usable
_CITY_STAY_FALLBACKS: dict[str, dict[str, str]] = {
    "los angeles": {
        "name": "The Hollywood Roosevelt",
        "image_query": "Hollywood Roosevelt Hotel 2015",
        "features": "Hollywood Blvd · pool · walk to TCL Chinese Theatre",
    },
    "la": {
        "name": "The Hollywood Roosevelt",
        "image_query": "Hollywood Roosevelt Hotel 2015",
        "features": "Hollywood Blvd · pool · walk to TCL Chinese Theatre",
    },
    "san francisco": {
        "name": "Hotel Zephyr San Francisco",
        "image_query": "Fisherman's Wharf San Francisco waterfront",
        "features": "Fisherman's Wharf · bay views · Embarcadero",
    },
    "san diego": {
        "name": "Hotel del Coronado",
        "image_query": "Hotel del Coronado",
        "features": "Coronado Beach · historic icon · spa",
    },
    "tokyo": {
        "name": "Hotel Gracery Shinjuku",
        "image_query": "Shinjuku Tokyo skyline",
        "features": "Shinjuku · Godzilla Head · transit hub",
    },
    "seoul": {
        "name": "L7 Myeongdong",
        "image_query": "Myeongdong Seoul",
        "features": "Myeongdong · shopping · metro access",
    },
    "london": {
        "name": "The Z Hotel Piccadilly",
        "image_query": "Piccadilly Circus London",
        "features": "West End · Piccadilly Circus · compact city stay",
    },
    "miami": {
        "name": "The Confidante Miami Beach",
        "image_query": "Miami Beach hotel oceanfront",
        "features": "Miami Beach · Art Deco · oceanfront",
    },
    "orlando": {
        "name": "Universal's Cabana Bay Beach Resort",
        "image_query": "Orlando hotel resort pool",
        "features": "Near Universal · family resort · pool",
    },
}


def _fallback_stay_hotel(city: str) -> dict[str, str]:
    key = (city or "").strip().lower()
    fb = _CITY_STAY_FALLBACKS.get(key) or {}
    name = fb.get("name") or (f"Recommended hotel · {city}" if city else "Recommended hotel")
    return {
        "name": name,
        "image_url": _city_hotel_fallback_image(city, hotel_name=name),
        "features": fb.get("features", ""),
        "location": city,
        "stars": "4",
        "score": "8.4",
        "score_label": "Very Good",
    }


def _is_curated_fallback_name(name: str, city: str = "") -> bool:
    """True when ``name`` is a hard-coded city stub, not a live Trip.com title."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if city:
        fb = _CITY_STAY_FALLBACKS.get(city.strip().lower()) or {}
        if fb.get("name") and n == fb["name"].strip().lower():
            return True
    for fb in _CITY_STAY_FALLBACKS.values():
        if fb.get("name") and n == fb["name"].strip().lower():
            return True
    return False


def _default_depart(days_ahead: int = 21) -> str:
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def _default_return(days_ahead: int = 28) -> str:
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def _default_return_from(depart: str, nights: int = 7) -> str:
    """Return date = depart + nights (avoids accidental ~month-long stays)."""
    try:
        return (date.fromisoformat(depart) + timedelta(days=max(1, nights))).isoformat()
    except ValueError:
        return _default_return(days_ahead=28)


def _build_suggested_day_flow(
    nights: int,
    *,
    interests: str = "",
    destination: str = "",
    attractions: list[str] | None = None,
    attraction_plan: list[dict[str, str]] | None = None,
    regional_route: object | None = None,
    arrive_time: str = "",
    return_depart_time: str = "",
) -> str:
    """Emit one Day N block per trip day with named sights and meals."""
    from travel_agent.destination_guides import (
        day_ideas_for,
        format_day_body,
        format_day_title,
    )

    n = max(1, int(nights or 1))
    styles = [s.strip() for s in (interests or "First-time").split(",") if s.strip()]
    ideas = day_ideas_for(
        destination,
        n,
        styles=styles or None,
        attractions=attractions,
        attraction_plan=attraction_plan,
        regional_route=regional_route,
    )
    lines: list[str] = []
    for i, idea in enumerate(ideas, start=1):
        lines.append(format_day_title(idea, i) + ":")
        body = format_day_body(
            idea,
            arrive_time=arrive_time if i == 1 else "",
            return_depart_time=return_depart_time if i == n else "",
        )
        for bullet in body.splitlines():
            lines.append(bullet.replace("• ", "- ", 1) if bullet.startswith("• ") else f"- {bullet}")
    return "\n".join(lines)


_CAR_NEED_RE = re.compile(
    r"\b("
    r"rent(?:al)?\s*car|car\s*rental|hire\s*car|car\s*hire|"
    r"road\s*trip|self[- ]?drive|need(?:s)?\s+a?\s*car|driving"
    r")\b",
    re.IGNORECASE,
)


def _as_bool(value: bool | str | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _wants_rental_car(
    rent_car: bool | str | None = None,
    interests: str = "",
) -> bool:
    """True when the user explicitly wants a rental car or interests imply it."""
    if isinstance(rent_car, bool):
        return rent_car
    if isinstance(rent_car, str):
        token = rent_car.strip().lower()
        if token in {"1", "true", "yes", "y", "on"}:
            return True
        if token in {"0", "false", "no", "n", "off"}:
            return False
    blob = f"{rent_car or ''} {interests or ''}"
    return bool(_CAR_NEED_RE.search(blob))


def _fmt_hkd(value: float | None) -> str:
    return f"HK${value:,.0f}" if value is not None else "n/a"


def _clean_text(text: str, limit: int = 6000) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + "\n\n[...truncated...]"
    return text


_TIME_LINE_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_AIRPORT_LINE_RE = re.compile(r"^[A-Z]{3}$")
_DURATION_LINE_RE = re.compile(
    r"^\d+\s*h(?:ours?)?(?:\s*\d+\s*m(?:ins?)?)?|\d+h\s*\d+m",
    re.I,
)
_FLIGHT_ROW_SKIP = frozenset(
    {
        "recommended",
        "cheapest",
        "direct first",
        "sort by",
        "return",
        "select",
        "create price alert",
        "show more",
        "alliance",
        "airlines",
        "times",
        "duration",
        "stops",
        "airports",
        "cabin",
        "economy",
        "business",
        "first",
        "any",
        "direct",
        "<9 left",
        "cheapest direct",
    }
)


def _is_filter_time(time_str: str) -> bool:
    """Sidebar filter placeholders — not real flight clock times."""
    return time_str in {"00:00", "24:00"}


def parse_trip_com_flight_rows(
    body: str,
    *,
    origin: str = "",
    destination: str = "",
) -> list[dict[str, str]]:
    """Parse Trip.com flight result rows from visible page text."""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    start = 0
    # Prefer inbound ("Returning to") header when present; else last "flights found"
    for i, ln in enumerate(lines):
        if re.search(r"(?i)Returning\s+to\b", ln):
            start = i
            break
    else:
        for i, ln in enumerate(lines):
            if re.search(r"\d+\s+flights found", ln, re.I) or re.search(
                r"(?i)Departures\s+to\b", ln
            ):
                start = i

    rows: list[dict[str, str]] = []
    i = start
    while i < len(lines) - 5:
        ln = lines[i]
        low = ln.lower()
        if (
            not ln
            or low in _FLIGHT_ROW_SKIP
            or ln.startswith("HK$")
            or ln.startswith("<")
            or re.match(r"^\+?\d+$", ln)
            or re.search(r"(?i)^operated by\b", ln)
            or re.search(r"(?i)^checked baggage\b", ln)
        ):
            i += 1
            continue

        # Airline may be followed by "Operated by …" before the clock time
        k = i + 1
        while k < len(lines) and re.search(r"(?i)^operated by\b", lines[k]):
            k += 1
        if (
            k + 1 < len(lines)
            and _TIME_LINE_RE.match(lines[k])
            and not _is_filter_time(lines[k])
            and _AIRPORT_LINE_RE.match(lines[k + 1])
        ):
            airline = expand_airline_code(ln)
            if not is_plausible_airline_name(airline):
                i += 1
                continue
            dep = lines[k]
            from_ap = lines[k + 1]
            duration = ""
            stops = "Direct"
            arr = ""
            to_ap = ""
            price = ""
            j = k + 2
            while j < len(lines) and j < k + 18:
                cur = lines[j]
                if cur.startswith("HK$"):
                    price = cur.replace(" ", "")
                    break
                if re.search(r"(?i)^operated by\b", cur):
                    j += 1
                    continue
                if _DURATION_LINE_RE.match(cur) and not duration:
                    duration = re.sub(r"\s+", " ", cur).strip()
                elif re.search(r"(?i)\bdirect\b", cur):
                    stops = "Direct"
                elif re.search(r"(?i)\d+\s*stop", cur):
                    stop_m = re.search(r"(?i)(\d+)\s*stop", cur)
                    stops = f"{stop_m.group(1)} stop" if stop_m else "1 stop"
                elif re.search(
                    r"(?i)\d+\s*h(?:ours?)?\s*\d*\s*m?(?:ins?)?\s+in\s+\w+", cur
                ) or re.search(r"(?i)\d+h\s*\d*m\s+in\s+", cur):
                    # e.g. "2h 19m in San Francisco"
                    lay = re.sub(r"\s+", " ", cur).strip()
                    if stops == "Direct":
                        stops = f"1 stop · {lay}"
                    elif "·" not in stops:
                        stops = f"{stops} · {lay}"
                elif " in " in cur.lower() and "stop" not in stops.lower():
                    stops = "1 stop"
                elif _TIME_LINE_RE.match(cur) and not _is_filter_time(cur) and not arr:
                    arr = cur
                elif _AIRPORT_LINE_RE.match(cur) and arr and not to_ap:
                    to_ap = cur
                j += 1
            if arr and dep:
                rows.append(
                    {
                        "airline": airline,
                        "depart_time": dep,
                        "arrive_time": arr,
                        "depart_airport": from_ap or origin.upper(),
                        "arrive_airport": to_ap or destination.upper(),
                        "duration": duration,
                        "stops": stops,
                        "price_label": price,
                    }
                )
            i = max(i + 1, j)
            continue
        i += 1
    return rows


_CAR_VENDORS = (
    "FAT UNCLE CAR RENTAL",
    "Hertz",
    "Avis",
    "Budget",
    "Sixt",
    "Enterprise",
    "Alamo",
    "National",
    "Dollar",
    "Thrifty",
    "Europcar",
    "TOYOTA Rent a Car",
    "Times",
    "nu",
    "Nu Car Rentals",
    "Ace",
    "Fox",
    "Payless",
)

_CAR_BRANDS = (
    "Dodge|Toyota|Nissan|Ford|Chevrolet|Chevy|Jeep|Kia|Audi|BMW|Mercedes|"
    "Honda|Hyundai|Tesla|Chrysler|Volkswagen|VW|Mazda|Subaru|Lexus|Volvo|"
    "Porsche|Buick|GMC|Mitsubishi|Peugeot|Renault|Fiat|Mini|Land Rover|"
    "Range Rover|Cadillac|Lincoln|Ram|Jaguar|Infiniti|Acura|Genesis"
)


def _parse_car_chunk(chunk: str) -> dict[str, str]:
    """Parse one car listing block from Trip.com car hire results."""
    blob = chunk or ""
    card: dict[str, str] = {
        "name": "",
        "similar": "",
        "vendor": "",
        "score": "",
        "reviews": "",
        "seats": "",
        "fuel": "",
        "pickup_note": "",
        "cancellation": "",
        "mileage": "",
        "payment": "",
        "insurance": "",
        "price_label": "",
        "total_label": "",
        "url": "",
    }

    name_m = re.search(
        r"(?i)([A-Z][A-Za-z0-9 \-]+?)\s+(or similar\s+[A-Za-z ]+)",
        blob,
    )
    if name_m:
        card["name"] = name_m.group(1).strip()
        card["similar"] = name_m.group(2).strip()
    if not card["name"]:
        name_m = re.search(
            rf"(?i)({_CAR_BRANDS})\s+[A-Za-z0-9\-]+(?:\s+[A-Za-z0-9\-]+)?",
            blob,
        )
        if name_m:
            card["name"] = name_m.group(0).strip()
    sim_m = re.search(r"(?i)or similar\s+[A-Za-z ]+", blob)
    if sim_m and not card["similar"]:
        card["similar"] = sim_m.group(0).strip()

    score_m = re.search(r"\b([6-9](?:\.\d)?|10(?:\.0)?)\s*/\s*10\b", blob)
    if score_m:
        card["score"] = f"{score_m.group(1)}/10"
    rev_m = re.search(r"(?i)(\d[\d,]*)\s*review", blob)
    if rev_m:
        card["reviews"] = f"{rev_m.group(1)} review(s)"

    # Specs: seats / bags / Automatic (Trip.com list icons)
    seats_m = re.search(
        r"(?i)(?:^|[^\d])([2-9]|1[0-5])\s*(?:seats?|passengers?|pax)?\b",
        blob,
    )
    # Compact icon row often appears as "5 4 Automatic" near the title
    compact = re.search(
        r"(?i)\b([2-9]|1[0-5])\s+([1-9]|1[0-5])\s+(Automatic|Manual)\b",
        blob,
    )
    if compact:
        card["seats"] = compact.group(1)
        card["fuel"] = compact.group(3).title()
    elif seats_m:
        card["seats"] = seats_m.group(1)

    if re.search(r"(?i)\bAutomatic\b", blob) and not card.get("fuel"):
        card["fuel"] = "Automatic"
    elif re.search(r"(?i)\bManual\b", blob) and not card.get("fuel"):
        card["fuel"] = "Manual"
    elif re.search(r"(?i)\bElectric\b", blob):
        card["fuel"] = "Electric"
    elif re.search(r"(?i)\bHybrid\b", blob):
        card["fuel"] = "Hybrid"
    elif re.search(r"(?i)\bDiesel\b", blob):
        card["fuel"] = "Diesel"
    elif re.search(r"(?i)\bPetrol\b|\bGasoline\b", blob):
        card["fuel"] = "Petrol"

    seat_fuel = re.search(
        r"(?im)\b([2-9]|1[0-2])\s+(Electric|Hybrid|Petrol|Diesel|Gasoline|Automatic|Manual)\b",
        blob,
    )
    if seat_fuel and not card.get("seats"):
        card["seats"] = seat_fuel.group(1)
        fuel = seat_fuel.group(2)
        if not card.get("fuel"):
            card["fuel"] = "Petrol" if fuel.lower() == "gasoline" else fuel.title()
    elif seat_fuel and not card.get("fuel"):
        fuel = seat_fuel.group(2)
        card["fuel"] = "Petrol" if fuel.lower() == "gasoline" else fuel.title()

    if re.search(r"(?i)airport train", blob):
        card["pickup_note"] = "Airport train to the counter"
    elif re.search(r"(?i)free shuttle", blob):
        card["pickup_note"] = "Free shuttle to counter"
    elif re.search(r"(?i)in[- ]terminal|meet and greet|counter", blob):
        m = re.search(
            r"(?i)(Free shuttle[^.!\n]+|In-terminal[^.!\n]+|Meet and greet[^.!\n]+|"
            r"Airport train[^.!\n]+)",
            blob,
        )
        if m:
            card["pickup_note"] = m.group(1).strip()[:80]

    cancel_m = re.search(
        r"(?i)((?:Free cancellation|Cancellation with fee)[^.!\n]*)",
        blob,
    )
    if cancel_m:
        card["cancellation"] = cancel_m.group(1).strip()[:100]
    mile_m = re.search(
        r"(?i)((?:\d[\d,]*)\s*(?:mi|km|miles)\s+per\s+(?:rental|day)|Unlimited mileage)",
        blob,
    )
    if mile_m:
        card["mileage"] = mile_m.group(1).strip()[:80]
    if re.search(r"(?i)prepay online", blob):
        card["payment"] = "Prepay online"
    elif re.search(r"(?i)pay at pick[- ]?up", blob):
        card["payment"] = "Pay at pick-up"
    ins_m = re.search(
        r"(?i)(Includes? (?:Third Party Liability|CDW|collision|insurance)[^.!\n]*)",
        blob,
    )
    if ins_m:
        card["insurance"] = ins_m.group(1).strip()[:100]

    # Vendor — "nu Multinational chain" style
    chain_m = re.search(
        r"(?i)\b([A-Za-z][A-Za-z0-9 &.'\-]{1,40})\s+Multinational chain\b",
        blob,
    )
    if chain_m:
        card["vendor"] = f"{chain_m.group(1).strip()} · Multinational chain"
    else:
        for vendor in _CAR_VENDORS:
            # Avoid matching "National" inside "Multinational chain"
            if vendor.lower() == "national" and "multinational" in blob.lower():
                continue
            if re.search(rf"(?i)\b{re.escape(vendor)}\b", blob):
                card["vendor"] = (
                    vendor.title() if vendor.isupper() and len(vendor) > 8 else vendor
                )
                break

    daily_m = re.search(
        r"(?i)HK\s*\$?\s*([0-9,]+(?:\.\d+)?)\s*/\s*day",
        blob,
    )
    if daily_m:
        card["price_label"] = f"HK${daily_m.group(1).replace(',', '')}"
    else:
        # Sometimes shown as HK$259 without /day next to Total
        daily_m = re.search(
            r"(?i)(?:^|[^\d])HK\s*\$?\s*([0-9]{2,5}(?:,\d{3})?(?:\.\d+)?)\b(?!\s*Total)",
            blob,
        )
        if daily_m and int(daily_m.group(1).replace(",", "").split(".")[0]) < 5000:
            card["price_label"] = f"HK${daily_m.group(1).replace(',', '')}"
    total_m = re.search(r"(?i)Total\s*HK\s*\$?\s*([0-9,]+(?:\.\d+)?)", blob)
    if total_m:
        card["total_label"] = f"Total HK${total_m.group(1)}"
    return card


def parse_trip_com_car_rows(body: str, *, limit: int = 8) -> list[dict[str, str]]:
    """Parse multiple car rental deals from Trip.com car hire list text."""
    blob = (body or "")[:20000]
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    chunks = re.split(r"(?i)\bview deal\b", blob)
    if len(chunks) < 2:
        chunks = [blob]

    for chunk in chunks:
        card = _parse_car_chunk(chunk)
        key = (card.get("name", ""), card.get("price_label", ""))
        if key in seen or not (card.get("name") or card.get("price_label")):
            continue
        seen.add(key)
        rows.append(card)
        if len(rows) >= limit:
            return rows

    for m in re.finditer(
        r"(?is).{0,420}HK\s*\$?\s*[\d,]+(?:\.\d+)?\s*/\s*day",
        blob,
    ):
        card = _parse_car_chunk(m.group(0))
        key = (card.get("name", ""), card.get("price_label", ""))
        if key in seen or not (card.get("name") or card.get("price_label")):
            continue
        seen.add(key)
        rows.append(card)
        if len(rows) >= limit:
            break
    return rows


def _pick_flight_row_cheapest(
    rows: list[dict[str, str]], *, lowest: float | None = None
) -> dict[str, str] | None:
    """Cheapest-row fallback when LLM selection is unavailable."""
    if not rows:
        return None
    priced: list[tuple[dict[str, str], float]] = []
    for row in rows:
        prices = parse_prices(row.get("price_label", ""))
        if prices:
            priced.append((row, prices[0]))
    if priced:
        return min(priced, key=lambda x: x[1])[0]
    return rows[0]


class TripBrowser:
    """Controls a Chromium session pointed at Trip.com Hong Kong."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.page: Page | None = None
        self.last_attractions: list[str] = []
        # Rich Trip.com attraction cards (name/url/image/address/hours/visit_time)
        self.last_attraction_cards: list[dict[str, str]] = []
        # LLM-arranged day-by-day sightseeing route (title/go/also/lunch/dinner/route)
        self.last_attraction_day_plan: list[dict[str, str]] = []
        self.last_hotel_stays: list[dict[str, str]] = []
        # Live /hotels/list URL from the last Playwright type→autocomplete→Search
        self.last_hotel_list_url: str = ""
        # Preferred booking link: a specific hotel detail page (hotelId=…)
        self.last_hotel_detail_url: str = ""
        # Real hotel title scraped from that detail page
        self.last_hotel_name: str = ""
        self.last_proposed_route: object | None = None
        self.last_flight_card: dict[str, str] = {}
        # Frozen after plan_trip so later search_flights calls cannot wipe open-jaw times
        self.last_plan_flight_card: dict[str, str] = {}
        # Candidate lists for LLM ranking (flight rows / hotel options)
        self.last_flight_candidates: list[dict[str, str]] = []
        self.last_hotel_candidates: list[dict[str, str]] = []
        # Car rental deal from Playwright carhire search
        self.last_car_card: dict[str, str] = {}
        self.last_car_detail_url: str = ""
        # LLM ranking context (budget, style, interests) for flight/hotel picks
        self.selection_context: SelectionContext = SelectionContext()
        self.llm_model: str = settings.ollama_model
        self.llm_host: str = settings.ollama_host
        self._home_warmup_error: str = ""

    def _update_selection_context(self, **kwargs: Any) -> None:
        """Merge trip prefs used when the LLM ranks scraped flights/hotels."""
        ctx = self.selection_context
        for key, val in kwargs.items():
            if val is None or val == "":
                continue
            if hasattr(ctx, key):
                setattr(ctx, key, val)

    def _pick_flight_row(
        self, rows: list[dict[str, str]], *, lowest: float | None = None
    ) -> dict[str, str] | None:
        """Pick a flight row — LLM ranks candidates; cheapest is the fallback."""
        if not rows:
            return None
        picked = select_flight_row(
            rows,
            self.selection_context,
            model=self.llm_model,
            host=self.llm_host,
            lowest=lowest,
        )
        return picked or _pick_flight_row_cheapest(rows, lowest=lowest)

    def _pick_hotel_from_options(
        self,
        options: list[tuple[str, str]],
        *,
        page_text: str = "",
        city: str = "",
        prices: list[float] | None = None,
    ) -> tuple[str, str]:
        """LLM-pick a hotel from list-page (url, name) pairs."""
        if not options:
            return "", ""
        candidates = enrich_hotel_candidates_from_page(
            options,
            page_text,
            city=city,
            default_prices=prices,
        )
        chosen = select_hotel_candidate(
            candidates,
            self.selection_context,
            model=self.llm_model,
            host=self.llm_host,
        )
        if chosen:
            return chosen.get("url", options[0][0]), chosen.get("name", options[0][1])
        return options[0]

    def _pick_car_candidate(
        self, candidates: list[dict[str, str]]
    ) -> dict[str, str] | None:
        """LLM-pick a car rental from scraped list candidates."""
        if not candidates:
            return None
        picked = select_car_candidate(
            candidates,
            self.selection_context,
            model=self.llm_model,
            host=self.llm_host,
        )
        return picked or candidates[0]

    def _arrange_attractions_route(
        self,
        attractions: list[str],
        nights: int,
        *,
        city: str = "",
        cards: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]] | None:
        """LLM-plan a day schedule from scraped Trip.com attractions (with details)."""
        cards = cards if cards is not None else list(self.last_attraction_cards or [])
        if not attractions and not cards:
            self.last_attraction_day_plan = []
            return None
        plan = arrange_attraction_route(
            attractions or [c.get("name", "") for c in cards],
            nights,
            self.selection_context,
            city=city,
            model=self.llm_model,
            host=self.llm_host,
            cards=cards,
            # Always LLM-schedule when we have real Trip.com attraction details
            force_llm=bool(cards),
        )
        if plan:
            self.last_attraction_day_plan = plan
            return plan
        self.last_attraction_day_plan = []
        return None

    def _collect_car_candidates(
        self,
        body: str,
        *,
        location: str = "",
        pickup_date: str = "",
        dropoff_date: str = "",
        prices: list[float] | None = None,
    ) -> list[dict[str, str]]:
        """Gather multiple car deals from the current results page for LLM ranking."""
        options = self._extract_car_detail_options(limit=8)
        text_rows = parse_trip_com_car_rows(body, limit=8)
        candidates: list[dict[str, str]] = []

        if options:
            candidates = enrich_car_candidates_from_page(
                options,
                body,
                location=location,
                pickup_date=pickup_date,
                dropoff_date=dropoff_date,
                default_prices=prices,
            )
        elif text_rows:
            candidates = [dict(row) for row in text_rows]

        if options and text_rows:
            for i, row in enumerate(candidates):
                if i < len(text_rows):
                    for key, val in text_rows[i].items():
                        if val and not row.get(key):
                            row[key] = val
        elif text_rows and not candidates:
            candidates = [dict(row) for row in text_rows]

        for row in candidates:
            row.setdefault("location", location)
            row.setdefault("pickup_date", pickup_date)
            row.setdefault("dropoff_date", dropoff_date)

        if not candidates and body.strip():
            one = _parse_car_chunk(body[:14000])
            if one.get("name") or one.get("price_label"):
                one.setdefault("location", location)
                one.setdefault("pickup_date", pickup_date)
                one.setdefault("dropoff_date", dropoff_date)
                candidates = [one]
        return candidates

    def start(self) -> None:
        self._pw = sync_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        last_err: Exception | None = None
        # Prefer Playwright Chromium; fall back to installed Chrome / Edge
        # (important for the frozen LocalLLMTravelAgent.exe when browsers live in ms-playwright).
        for kwargs in (
            {"headless": settings.headless, "args": launch_args},
            {"channel": "chrome", "headless": settings.headless, "args": launch_args},
            {"channel": "msedge", "headless": settings.headless, "args": launch_args},
        ):
            try:
                self._browser = self._pw.chromium.launch(**kwargs)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                continue
        if self._browser is None:
            hint = (
                "Playwright Chromium is missing. In a terminal run:\n"
                "  python -m playwright install chromium\n"
                "Or install Google Chrome / Microsoft Edge and try again."
            )
            raise RuntimeError(f"{hint}\n\nLast error: {last_err}") from last_err
        context = self._browser.new_context(
            locale="en-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        self.page = context.new_page()
        self.page.set_default_timeout(settings.browser_timeout_ms)
        # Warm-up navigation must not kill app startup (Trip.com flakes / VPN / DNS).
        try:
            self.open_home()
        except Exception as exc:
            # Browser is still usable; first search will retry navigation.
            self._home_warmup_error = str(exc)

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self.page = None
        self._browser = None
        self._pw = None

    def _require_page(self) -> Page:
        if not self.page:
            raise RuntimeError("Browser is not started. Call start() first.")
        return self.page

    def _safe_goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 45000,
        retries: int = 3,
    ) -> None:
        """Navigate with retries when Trip.com interrupts or the connection drops."""
        page = self._require_page()
        last_err: Exception | None = None
        retry_markers = (
            "interrupted",
            "navigation",
            "err_connection",
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_timed_out",
            "err_network_changed",
            "err_internet_disconnected",
            "err_empty_response",
            "err_ssl",
            "timeout",
            "net::",
        )
        for attempt in range(max(1, retries)):
            try:
                page.goto(url, wait_until=wait_until, timeout=timeout)
                return
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                if not any(m in msg for m in retry_markers):
                    raise
                # Let the interrupting navigation settle, then retry the target URL
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(800 + attempt * 600)
                # Soft reload between connection drops
                if "err_connection" in msg or "net::" in msg:
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
        if last_err:
            raise last_err
    def open_home(self) -> str:
        page = self._require_page()
        self._safe_goto(TRIP_HOME)
        page.wait_for_timeout(1500)
        return f"Opened Trip.com Hong Kong home: {page.url}"

    def scrape_attractions(self, city: str, *, limit: int = 18) -> list[str]:
        """Search Trip.com Attractions tab and enrich from detail pages.

        Flow: things-to-do search → Attractions tab → card links → HTTP detail
        (name, photo, address, open hours, recommended sightseeing time).
        """
        page = self._require_page()
        city_name = typed_place_name(to_hotel_city(city) or city)
        if not city_name:
            self.last_attractions = []
            self.last_attraction_cards = []
            return []

        from travel_agent.places import to_hotel_city_id
        from travel_agent.attraction_images import set_trip_attraction_images

        city_id = ""
        try:
            city_id = to_hotel_city_id(city_name) or ""
        except Exception:
            city_id = ""

        candidates = [
            (
                f"{settings.trip_base_url}/things-to-do/list?"
                f"{urlencode({'keyword': city_name, 'locale': settings.trip_locale, 'curr': settings.trip_currency})}"
            ),
            (
                f"{settings.trip_base_url}/things-to-do/?"
                f"{urlencode({'keyword': city_name, 'locale': settings.trip_locale, 'curr': settings.trip_currency})}"
            ),
        ]
        if city_id:
            candidates.insert(
                0,
                (
                    f"{settings.trip_base_url}/things-to-do/list?"
                    f"{urlencode({'districtId': city_id, 'locale': settings.trip_locale, 'curr': settings.trip_currency})}"
                ),
            )

        nav_skip = re.compile(
            r"(?i)^(attractions?\s*&\s*tours|attractions|experiences|must-have|"
            r"top picks|filters?|search|book now|view all|see all|more|"
            r"hotels?\s*&\s*homes|flights?|trains?|cars?|app)$"
        )
        product_skip = re.compile(
            r"(?i)\b(eSIM|SIM|wifi|wi-fi|JR Pass|airport express|lounge|voucher|"
            r"private car|charter|transfer bus|gift card|insurance|"
            r"go-kart|go kart|day trip from|day tour|unlimited|metro\s+\d|"
            r"skyliner|limousine bus)\b"
        )

        cards: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        seen_names: set[str] = set()

        def _clean_label(raw: str) -> str:
            text = re.sub(r"\s+", " ", (raw or "").strip())
            text = text.split("\n")[0].strip()
            text = re.sub(r"^(No\.\s*\d+\s+of\s+.+?:\s*)", "", text, flags=re.I)
            return text

        def _add_card(name: str, href: str, image_url: str = "") -> None:
            label = _clean_label(name)
            if not label or len(label) < 3 or len(label) > 90:
                return
            if nav_skip.match(label) or product_skip.search(label):
                return
            if re.fullmatch(r"[\d.,\sHK$%]+", label):
                return
            if label.count("·") > 2 or label.count("|") > 2:
                return
            url = (href or "").strip()
            if url.startswith("/"):
                url = f"{settings.trip_base_url}{url}"
            if "/travel-guide/attraction/" not in url.lower():
                return
            url = url.split("#")[0]
            # Drop tracking noise but keep locale
            if "locale=" not in url.lower():
                sep = "&" if "?" in url else "?"
                url = (
                    f"{url}{sep}locale={settings.trip_locale}"
                    f"&curr={settings.trip_currency}"
                )
            key_u = re.sub(r"[?&](lasttraceid|ext-[^=]+)=[^&]*", "", url.lower())
            key_n = _attraction_base_key(label)
            if key_u in seen_urls or key_n in seen_names:
                return
            seen_urls.add(key_u)
            seen_names.add(key_n)
            cards.append(
                {
                    "name": label,
                    "url": url,
                    "image_url": image_url or "",
                    "address": "",
                    "open_hours": "",
                    "visit_time": "",
                }
            )

        for url in candidates:
            try:
                self._safe_goto(url)
                page.wait_for_timeout(2000)
                # Prefer Attractions tab (not Experiences / tours)
                for tab_sel in (
                    "text=Attractions",
                    "[role='tab']:has-text('Attractions')",
                    "a:has-text('Attractions')",
                    "div:has-text('Attractions')",
                ):
                    try:
                        tab = page.locator(tab_sel).first
                        if tab.count():
                            tab.click(timeout=2500)
                            page.wait_for_timeout(1800)
                            break
                    except Exception:
                        continue

                # Collect attraction detail anchors only
                locs = page.locator("a[href*='/travel-guide/attraction/']")
                count = min(locs.count(), 48)
                for i in range(count):
                    try:
                        a = locs.nth(i)
                        href = (a.get_attribute("href") or "").strip()
                        label = ""
                        try:
                            label = (a.inner_text(timeout=700) or "").strip()
                        except Exception:
                            label = ""
                        if not label:
                            label = (a.get_attribute("title") or "").strip()
                        if not label:
                            label = (a.get_attribute("aria-label") or "").strip()
                        img = ""
                        try:
                            img_el = a.locator("img").first
                            if img_el.count():
                                img = (
                                    img_el.get_attribute("src")
                                    or img_el.get_attribute("data-src")
                                    or ""
                                ).strip()
                        except Exception:
                            img = ""
                        _add_card(label, href, img)
                        if len(cards) >= max(limit, 8):
                            break
                    except Exception:
                        continue
                if len(cards) >= max(6, limit // 2):
                    break
            except Exception:
                continue

        # Enrich from detail pages (photo, address, hours, visit time)
        enrich_n = min(len(cards), max(6, min(int(limit or 18), 10)))
        for card in cards[:enrich_n]:
            meta = _http_fetch_attraction_detail_meta(card.get("url", ""))
            if not meta:
                continue
            if meta.get("name") and len(meta["name"]) >= 3:
                # Prefer detail poiName when list text was noisy
                if (
                    not card["name"]
                    or len(card["name"]) > 70
                    or nav_skip.match(card["name"])
                ):
                    card["name"] = meta["name"]
                elif meta["name"].lower() not in card["name"].lower():
                    # Keep shorter landmark-style name from detail when list is a package
                    if len(meta["name"]) < len(card["name"]):
                        card["name"] = meta["name"]
            if meta.get("image_url"):
                card["image_url"] = meta["image_url"]
            if meta.get("address"):
                card["address"] = meta["address"]
            if meta.get("open_hours"):
                card["open_hours"] = meta["open_hours"]
            if meta.get("visit_time"):
                card["visit_time"] = meta["visit_time"]

        # Drop any leftover chrome / product rows after enrich
        cleaned_cards: list[dict[str, str]] = []
        seen_final: set[str] = set()
        for card in cards:
            name = _clean_label(card.get("name", ""))
            if not name or nav_skip.match(name) or product_skip.search(name):
                continue
            key = _attraction_base_key(name)
            if key in seen_final:
                continue
            seen_final.add(key)
            card["name"] = name
            cleaned_cards.append(card)
            if len(cleaned_cards) >= limit:
                break

        from travel_agent.destination_guides import filter_attraction_cards_for_destination

        cleaned_cards = filter_attraction_cards_for_destination(cleaned_cards, city_name)
        self.last_attraction_cards = cleaned_cards
        self.last_attractions = [c["name"] for c in cleaned_cards]
        try:
            set_trip_attraction_images(cleaned_cards)
        except Exception:
            pass
        return self.last_attractions

    def search_attractions(self, city: str, limit: int = 18) -> str:
        """Tool wrapper: list Trip.com attractions with detail fields for a city."""
        names = self.scrape_attractions(city, limit=max(6, int(limit or 18)))
        cards = list(self.last_attraction_cards or [])
        city_name = to_hotel_city(city) or city
        n = int(self.selection_context.nights or 0)
        if (names or cards) and n > 0:
            self._arrange_attractions_route(names, n, city=city_name, cards=cards)
        if not names:
            return (
                f"No attractions parsed for {city_name} on Trip.com things-to-do. "
                f"Try https://hk.trip.com/things-to-do/?locale=en-HK&curr=HKD"
            )
        lines = [
            f"TRIP.COM ATTRACTIONS — {city_name}",
            f"Source: https://hk.trip.com/things-to-do/list?keyword={city_name}"
            f"&locale={settings.trip_locale}&curr={settings.trip_currency}",
            "Tab: Attractions (detail pages scraped for photo / address / hours)",
            "",
        ]
        for i, card in enumerate(cards or [{"name": n} for n in names], 1):
            name = card.get("name") or ""
            bits = [f"{i}. {name}"]
            if card.get("visit_time"):
                bits.append(f"Visit: {card['visit_time']}")
            if card.get("open_hours"):
                bits.append(f"Open: {card['open_hours']}")
            if card.get("address"):
                bits.append(f"Address: {card['address']}")
            if card.get("url"):
                bits.append(f"URL: {card['url']}")
            lines.append(" | ".join(bits))
        if self.last_attraction_day_plan:
            lines.append("")
            lines.append("LLM day schedule (from attraction list):")
            for i, day in enumerate(self.last_attraction_day_plan, 1):
                go = day.get("go", "")
                also = day.get("also", "")
                pair = f"{go} + {also}" if also and also != go else go
                lines.append(f"  Day {i}: {pair}")
        lines.append("")
        lines.append(
            "Use these exact attraction names (with hours/visit time) in the "
            "Day-by-day itinerary — never write generic 'Attractions & Tours'."
        )
        return "\n".join(lines)

    def get_page_summary(self) -> str:
        page = self._require_page()
        title = page.title()
        url = page.url
        body = page.inner_text("body")
        return _clean_text(f"Title: {title}\nURL: {url}\n\nVisible page text:\n{body}")

    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
        return_date: str | None = None,
        trip_type: str = "oneway",
        adults: int = 1,
        include_return_leg: bool = True,
    ) -> str:
        """Search flights on Trip.com HK and return visible result text.

        Flow: load fare list → form candidate rows → LLM picks one → fill card.
        """
        page = self._require_page()
        origin_raw = origin.strip()
        dest_raw = destination.strip()
        origin = to_flight_code(origin_raw)
        destination = to_flight_code(dest_raw)
        depart_date = depart_date or _default_depart()
        trip_type_norm = trip_type.strip().lower()
        is_round = trip_type_norm in {"round", "roundtrip", "rt", "2"}
        adults_n = max(1, min(int(adults or 1), 9))
        self._update_selection_context(
            origin=origin_raw or origin,
            destination=dest_raw or destination,
            adults=adults_n,
            checkin=depart_date,
            checkout=return_date or "",
        )

        params: dict[str, Any] = {
            "dcity": origin,
            "acity": destination,
            "ddate": depart_date,
            "triptype": "rt" if is_round else "ow",
            "class": "y",
            "quantity": adults_n,
            "searchboxarg": "t",
            "locale": settings.trip_locale,
            "curr": settings.trip_currency,
        }
        if is_round:
            try:
                params["rdate"] = return_date or (
                    date.fromisoformat(depart_date) + timedelta(days=7)
                ).isoformat()
            except ValueError:
                params["rdate"] = return_date or _default_return()

        canonical = f"{settings.trip_base_url}/flights/showfarefirst?{urlencode(params)}"
        api_prices: list[float] = []

        def _on_flight_api(response) -> None:  # type: ignore[no-untyped-def]
            try:
                url_l = response.url.lower()
                if "getlowpriceincalender" not in url_l and "getlowprice" not in url_l:
                    return
                if response.status != 200:
                    return
                data = response.json()
                blob = json.dumps(data, ensure_ascii=False)
                api_prices.extend(parse_prices(blob))
                # Also pull bare numeric price fields commonly used by Trip.com
                for m in re.finditer(
                    r'"(?:price|salePrice|totalPrice|lowestPrice|amount)"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
                    blob,
                ):
                    try:
                        val = float(m.group(1))
                    except ValueError:
                        continue
                    if 200 <= val <= 500_000:
                        api_prices.append(val)
            except Exception:
                return

        page.on("response", _on_flight_api)
        self._safe_goto(canonical)
        page.wait_for_timeout(4000)
        self._dismiss_popups()
        # Fares hydrate after calendar/API calls — wait until listing rows appear
        for _ in range(36):
            try:
                probe = page.inner_text("body")
            except Exception:
                probe = ""
            rows = parse_trip_com_flight_rows(
                probe, origin=origin.upper(), destination=destination.upper()
            )
            if rows and (parse_prices(probe) or api_prices):
                break
            page.wait_for_timeout(1000)
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(2500)
        self._dismiss_popups()
        try:
            page.remove_listener("response", _on_flight_api)
        except Exception:
            pass

        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        snippet = self._extract_flightish_content()
        prices = parse_prices(body) or parse_prices(snippet)
        if not prices and api_prices:
            prices = sorted(set(round(p, 2) for p in api_prices))[:12]
        price_note = (
            f"Parsed prices (HKD-like): {prices[:10]}"
            if prices
            else "Parsed prices: NONE — page may still be loading or blocked."
        )
        # Prefer full body when it has prices (main selector can miss late-loaded fares)
        content = body if prices and len(body) > 200 else snippet
        final_url = page.url if "trip.com" in page.url else canonical

        # Explicit list → LLM pick → card (also stores last_flight_candidates)
        candidate_rows = parse_trip_com_flight_rows(
            body or content, origin=origin.upper(), destination=destination.upper()
        )
        self.last_flight_candidates = [dict(r) for r in candidate_rows[:12]]
        card = self._scrape_top_flight_card(
            origin=origin.upper(),
            destination=destination.upper(),
            lowest=prices[0] if prices else None,
        )
        # If scrape missed times but list had rows, force the LLM pick onto the card
        if candidate_rows and not card.get("depart_time"):
            picked = self._pick_flight_row(
                candidate_rows, lowest=prices[0] if prices else None
            )
            if picked:
                card.update({k: v for k, v in picked.items() if v})
        candidates_block = ""
        if self.last_flight_candidates:
            lines = ["Flight candidates (LLM selects one):"]
            for i, row in enumerate(self.last_flight_candidates[:8]):
                lines.append(
                    f"  {i}. {row.get('airline', '?')} "
                    f"{row.get('depart_time', '?')}→{row.get('arrive_time', '?')} "
                    f"{row.get('price_label', '')} "
                    f"[{row.get('stops', 'Direct')}]"
                )
            candidates_block = "\n".join(lines) + "\n"
        if is_round and include_return_leg and card.get("depart_time"):
            ret_card = self._scrape_return_flight_card(
                outbound=card,
                origin=destination.upper(),
                destination=origin.upper(),
            )
            if not ret_card:
                # Fallback: search the return date as a one-way reverse sector
                try:
                    rev = self.search_flights(
                        origin=destination,
                        destination=origin,
                        depart_date=str(params.get("rdate") or return_date or ""),
                        trip_type="oneway",
                        adults=adults_n,
                        include_return_leg=False,
                    )
                    rev_fields = extract_booking_urls(rev)
                    ret_card = {
                        "airline": rev_fields.get("flight_airline", ""),
                        "airline_logo": rev_fields.get("flight_airline_logo", ""),
                        "depart_time": rev_fields.get("flight_depart", ""),
                        "arrive_time": rev_fields.get("flight_arrive", ""),
                        "depart_airport": rev_fields.get("flight_from", ""),
                        "arrive_airport": rev_fields.get("flight_to", ""),
                        "duration": rev_fields.get("flight_duration", ""),
                        "stops": rev_fields.get("flight_stops", ""),
                        "price_label": rev_fields.get("flight_price", ""),
                    }
                    # Restore outbound search URL context for booking link
                    try:
                        self._safe_goto(canonical)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                except Exception:
                    ret_card = {}
            if ret_card:
                for key, val in ret_card.items():
                    if val:
                        card[f"return_{key}"] = val
                # Keep round-trip package price from outbound list when available
                if not card.get("price_label") and ret_card.get("price_label"):
                    card["price_label"] = ret_card["price_label"]
        card["depart_date"] = depart_date
        if is_round and params.get("rdate"):
            card["return_date"] = str(params["rdate"])
        # Remember for the GUI even when the LLM rewrite drops times
        self.last_flight_card = {
            "flight_airline": card.get("airline", ""),
            "flight_airline_logo": card.get("airline_logo", ""),
            "flight_date": card.get("depart_date", ""),
            "flight_depart": card.get("depart_time", ""),
            "flight_arrive": card.get("arrive_time", ""),
            "flight_from": card.get("depart_airport", origin.upper()),
            "flight_to": card.get("arrive_airport", destination.upper()),
            "flight_duration": card.get("duration", ""),
            "flight_stops": card.get("stops", "Direct"),
            "flight_price": card.get("price_label", ""),
            "flight_return_airline": card.get("return_airline", ""),
            "flight_return_airline_logo": card.get("return_airline_logo", ""),
            "flight_return_date": card.get("return_date", ""),
            "flight_return_depart": card.get("return_depart_time", ""),
            "flight_return_arrive": card.get("return_arrive_time", ""),
            "flight_return_from": card.get("return_depart_airport", ""),
            "flight_return_to": card.get("return_arrive_airport", ""),
            "flight_return_duration": card.get("return_duration", ""),
            "flight_return_stops": card.get("return_stops", ""),
            "flight": ensure_locale_curr(canonical),
        }
        card_block = ""
        if card:
            airline_line = card.get("airline") or ""
            logo_line = card.get("airline_logo") or ""
            card_block = (
                "Structured flight card:\n"
                + (f"- Airline: {airline_line}\n" if airline_line else "")
                + (f"- Airline logo: {logo_line}\n" if logo_line else "")
                + (f"- Date: {card.get('depart_date', '')}\n" if card.get("depart_date") else "")
                + f"- Depart: {card.get('depart_time', '')}\n"
                f"- Arrive: {card.get('arrive_time', '')}\n"
                f"- From: {card.get('depart_airport', origin.upper())}\n"
                f"- To: {card.get('arrive_airport', destination.upper())}\n"
                f"- Duration: {card.get('duration', '')}\n"
                f"- Stops: {card.get('stops', 'Direct')}\n"
                f"- Price: {card.get('price_label', '')}\n"
            )
            if card.get("return_depart_time"):
                card_block += (
                    f"- Return airline: {card.get('return_airline', '')}\n"
                    + (
                        f"- Return airline logo: {card['return_airline_logo']}\n"
                        if card.get("return_airline_logo")
                        else ""
                    )
                    + (
                        f"- Return date: {card.get('return_date', '')}\n"
                        if card.get("return_date")
                        else ""
                    )
                    + f"- Return depart: {card.get('return_depart_time', '')}\n"
                    f"- Return arrive: {card.get('return_arrive_time', '')}\n"
                    f"- Return from: {card.get('return_depart_airport', destination.upper())}\n"
                    f"- Return to: {card.get('return_arrive_airport', origin.upper())}\n"
                    f"- Return duration: {card.get('return_duration', '')}\n"
                    f"- Return stops: {card.get('return_stops', '')}\n"
                )
        return _clean_text(
            f"Flight search URL: {ensure_locale_curr(normalize_trip_url(final_url))}\n"
            f"Canonical search URL: {ensure_locale_curr(canonical)}\n"
            f"Origin input: {origin_raw} -> code {origin.upper()}\n"
            f"Destination input: {dest_raw} -> code {destination.upper()}\n"
            f"Depart: {depart_date}"
            + (f" | Return: {params.get('rdate')}" if is_round else "")
            + f"\nTrip type: {'roundtrip' if is_round else 'oneway'}\n"
            + f"Adults: {adults_n}\n"
            + f"{price_note}\n"
            + (candidates_block if candidates_block else "")
            + (f"{card_block}\n" if card_block else "")
            + f"\n{content}"
        )

    def search_hotels(
        self,
        city: str,
        checkin: str | None = None,
        checkout: str | None = None,
        adults: int = 2,
        rooms: int = 1,
    ) -> str:
        """Search hotels by typing the city on Trip.com hub (Playwright autocomplete).

        Flow: open /hotels/ → type city → Search → form hotel list → LLM picks
        → open selected hotel DETAIL page → scrape name/score/price/image for the card.
        """
        page = self._require_page()
        # Human city name for typing — never URL-encoded "Los+Angeles"
        city_name = to_hotel_city(city) or typed_place_name(city)
        city_name = typed_place_name(city_name)
        checkin = checkin or _default_depart(14)
        try:
            checkout = checkout or (
                date.fromisoformat(checkin) + timedelta(days=7)
            ).isoformat()
        except ValueError:
            checkout = checkout or _default_return(17)
        adults_n = max(1, min(int(adults or 2), 8))
        rooms_n = max(1, min(int(rooms or 1), 8))

        self._update_selection_context(
            destination=city_name,
            checkin=checkin,
            checkout=checkout,
            adults=adults_n,
        )

        hub = (
            f"{settings.trip_base_url}/hotels/"
            f"?locale={settings.trip_locale}&curr={settings.trip_currency}"
        )

        filled = False
        for attempt in range(3):
            self._safe_goto(hub)
            page.wait_for_timeout(1800 + attempt * 500)
            self._dismiss_popups()
            filled = self._try_fill_hotel_form(
                city_name, checkin, checkout, adults_n, rooms_n
            )
            if filled and self._hotel_list_url_ok(page.url or ""):
                break
            filled = False

        # If still on hub, one more explicit type+Enter attempt
        if not self._hotel_list_url_ok(page.url or ""):
            try:
                self._safe_goto(hub)
                page.wait_for_timeout(1500)
                self._dismiss_popups()
                self._try_fill_hotel_form(
                    city_name, checkin, checkout, adults_n, rooms_n
                )
            except Exception:
                pass

        # Patch dates on the live list URL Trip.com built after autocomplete
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if self._hotel_list_url_ok(cur):
            patched = self._patch_hotel_list_dates(
                cur, checkin, checkout, adults_n, rooms_n
            )
            if patched and patched != cur:
                self._safe_goto(patched)
                page.wait_for_timeout(2000)
                self._dismiss_popups()
        else:
            # Hub autocomplete failed (common on later multi-city stays) — open
            # a constructed city list URL so we still get hotelId detail links.
            from travel_agent.trip_urls import build_hotel_list_url

            fallback_list = build_hotel_list_url(
                city_name,
                checkin,
                checkout,
                adults=adults_n,
                rooms=rooms_n,
            )
            try:
                self._safe_goto(fallback_list)
                page.wait_for_timeout(3500)
                self._dismiss_popups()
            except Exception:
                pass

        self._wait_for_results(
            keywords=["HK$", "HKD", "hotel", "guest", "star", "review", "night"],
            attempts=16,
        )
        for _ in range(12):
            try:
                probe = page.inner_text("body")
            except Exception:
                probe = ""
            found = [p for p in parse_prices(probe) if p >= 200]
            if found:
                break
            page.wait_for_timeout(1000)
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(2000)

        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        snippet = self._extract_list_content(
            selectors=[
                "[class*='hotel']",
                "[class*='Hotel']",
                "[class*='list']",
                "main",
                "body",
            ]
        )
        prices = [p for p in parse_prices(body) if p >= 200] or [
            p for p in parse_prices(snippet) if p >= 200
        ]
        price_note = (
            f"Parsed prices (HKD-like): {prices[:10]}"
            if prices
            else "Parsed prices: NONE — page may still be loading or blocked."
        )
        final_url = page.url if "trip.com" in (page.url or "") else hub

        # Keep the live list URL for reference
        if self._hotel_list_url_ok(final_url):
            list_url = ensure_locale_curr(normalize_trip_url(final_url))
            list_url = self._patch_hotel_list_dates(
                list_url, checkin, checkout, adults_n, rooms_n
            ) or list_url
        else:
            list_url = (
                f"{settings.trip_base_url}/hotels/"
                f"?locale={settings.trip_locale}&curr={settings.trip_currency}"
            )
            price_note += (
                f" | WARN: hub type-search did not reach a list URL for '{city_name}'."
            )

        self.last_hotel_list_url = list_url

        detail_links: list[str] = []
        hotel_names: list[str] = []
        raw_hotel_options: list[tuple[str, str]] = []
        for u, label in self._extract_hotel_detail_options(limit=10):
            nu = ensure_locale_curr(normalize_trip_url(u))
            low = nu.lower()
            if "/hotels/" not in low or "all-cities" in low:
                continue
            if "hotelid=" in low or re.search(r"hotel-detail-\d+", low) or re.search(
                r"/hotels/[^/?]+-\d+", low
            ):
                raw_hotel_options.append((nu, label))
                detail_links.append(nu)
                if label and label not in hotel_names:
                    hotel_names.append(label)
            if len(detail_links) >= 8:
                break

        # Form candidate list → LLM pick → open SELECTED detail → scrape card
        from travel_agent.llm_select import enrich_hotel_candidates_from_page
        from travel_agent.trip_urls import (
            canonicalize_hotel_detail_url,
            is_trusted_hotel_detail_url,
        )

        enriched: list[dict[str, str]] = []
        if raw_hotel_options:
            enriched = enrich_hotel_candidates_from_page(
                raw_hotel_options,
                body,
                city=city_name,
                default_prices=prices,
            )
            self.last_hotel_candidates = [dict(r) for r in enriched[:10]]
        else:
            self.last_hotel_candidates = []

        picked_url = ""
        picked_name = ""
        if len(raw_hotel_options) > 1:
            picked_url, picked_name = self._pick_hotel_from_options(
                raw_hotel_options,
                page_text=body,
                city=city_name,
                prices=prices,
            )
        elif raw_hotel_options:
            picked_url, picked_name = raw_hotel_options[0]

        if picked_url:
            detail_links = [picked_url] + [
                u for u, _ in raw_hotel_options if u != picked_url
            ]
        if picked_name:
            hotel_names = [picked_name] + [n for n in hotel_names if n != picked_name]

        if not detail_links:
            built = self._resolve_top_hotel_detail_url(
                checkin=checkin, checkout=checkout, adults=adults_n, rooms=rooms_n
            )
            if built:
                detail_links.append(built)
                picked_url = built

        rec_detail = ""
        if detail_links:
            rec_detail = (
                canonicalize_hotel_detail_url(
                    detail_links[0],
                    checkin=checkin,
                    checkout=checkout,
                    city=city_name,
                )
                or detail_links[0]
            )
            detail_links[0] = rec_detail
            picked_url = rec_detail
        if not is_trusted_hotel_detail_url(rec_detail):
            built = self._resolve_top_hotel_detail_url(
                checkin=checkin, checkout=checkout, adults=adults_n, rooms=rooms_n
            )
            if built:
                rec_detail = built
                picked_url = built
                if detail_links:
                    detail_links[0] = built
                else:
                    detail_links.append(built)
        self.last_hotel_detail_url = (
            rec_detail if is_trusted_hotel_detail_url(rec_detail) else ""
        )
        self.last_hotel_name = ""

        hotel_card: dict[str, str] = {
            "name": picked_name or (hotel_names[0] if hotel_names else ""),
            "stars": "",
            "score": "",
            "score_label": "",
            "reviews": "",
            "location": city_name,
            "price_label": f"HK${prices[0]:,.0f}" if prices else "",
            "image_url": "",
        }
        for row in enriched:
            if picked_url and row.get("url") == picked_url:
                for k in ("stars", "score", "reviews", "price_label", "location", "name"):
                    if row.get(k) and not hotel_card.get(k):
                        hotel_card[k] = row[k]
                break

        # Authoritative fields: list card + HTTP detail HTML (Playwright cannot
        # open /hotels/detail — it redirects to sign-in).
        if self.last_hotel_detail_url:
            meta = self._scrape_hotel_detail_meta(self.last_hotel_detail_url)
            for k, v in meta.items():
                if v:
                    hotel_card[k] = v
            # Detail-page title wins over list/LLM labels (link and name must match)
            if meta.get("name"):
                cleaned = clean_hotel_display_name(meta["name"], city_name)
                if cleaned and not cleaned.lower().startswith(
                    ("hotels in ", "recommended hotel")
                ):
                    self.last_hotel_name = cleaned
                    hotel_card["name"] = cleaned
            elif hotel_card.get("name") and _hotel_name_plausible_for_city(
                hotel_card["name"], city_name
            ):
                cleaned = clean_hotel_display_name(hotel_card["name"], city_name)
                hotel_card["name"] = cleaned
                if not cleaned.lower().startswith(
                    ("hotels in ", "recommended hotel")
                ):
                    self.last_hotel_name = cleaned
        elif hotel_card.get("name") and _hotel_name_plausible_for_city(
            hotel_card["name"], city_name
        ):
            cleaned = clean_hotel_display_name(hotel_card["name"], city_name)
            hotel_card["name"] = cleaned
            if not cleaned.lower().startswith(
                ("hotels in ", "recommended hotel")
            ):
                self.last_hotel_name = cleaned

        if not hotel_card.get("name") or hotel_card["name"].lower().startswith(
            ("hotels in ", "recommended hotel")
        ):
            list_card = self._scrape_top_hotel_card(
                city=city_name,
                fallback_name=hotel_card.get("name") or "",
                lowest=prices[0] if prices else None,
            )
            for k, v in list_card.items():
                if v and not hotel_card.get(k):
                    hotel_card[k] = v

        if hotel_card.get("name"):
            hotel_card["name"] = clean_hotel_display_name(
                hotel_card["name"], city_name
            )
        if self.last_hotel_name:
            self.last_hotel_name = clean_hotel_display_name(
                self.last_hotel_name, city_name
            )

        rec_name = self.last_hotel_name or hotel_card.get("name") or ""
        candidates_block = ""
        if self.last_hotel_candidates:
            lines = ["Hotel candidates (LLM selects one, then opens detail):"]
            for i, row in enumerate(self.last_hotel_candidates[:8]):
                lines.append(
                    f"  {i}. {row.get('name', '?')} | {row.get('score', '')} | "
                    f"{row.get('price_label', '')}"
                )
            if self.last_hotel_detail_url:
                lines.append(f"  → Selected detail: {self.last_hotel_detail_url}")
            candidates_block = "\n".join(lines) + "\n"

        booking_url = self.last_hotel_detail_url or list_url
        details = "\n".join(f"Hotel option link: {u}" for u in detail_links)
        card_block = ""
        if hotel_card:
            if self.last_hotel_detail_url and not hotel_card.get("image_url"):
                img = self.scrape_hotel_image_url(self.last_hotel_detail_url)
                if img:
                    hotel_card["image_url"] = img
            display_name = self.last_hotel_name or hotel_card.get("name") or rec_name
            card_block = (
                "Structured hotel card:\n"
                f"- Hotel: {display_name}\n"
                f"- Stars: {hotel_card.get('stars', '')}\n"
                f"- Score: {hotel_card.get('score', '')}\n"
                f"- Location: {hotel_card.get('location', city_name)}\n"
                + (
                    f"- Image: {hotel_card['image_url']}\n"
                    if hotel_card.get("image_url")
                    else ""
                )
                + f"- Nightly: {hotel_card.get('price_label', '')}\n"
                + f"- Reviews: {hotel_card.get('reviews', '')}\n"
            )
            rec_name = display_name or rec_name

        content = body if prices and len(body) > 200 else snippet
        how = (
            "Playwright: list → LLM pick → hotel DETAIL scrape"
            if self.last_hotel_detail_url
            else (
                "Playwright hub search: typed city → Search (list only)"
                if self._hotel_list_url_ok(list_url)
                else "Playwright hub search FAILED to resolve list (check city name)"
            )
        )
        return _clean_text(
            f"Hotel search method: {how}\n"
            f"City/keyword typed: {city_name}\n"
            f"Check-in: {checkin} | Check-out: {checkout}\n"
            f"Adults: {adults_n} | Rooms: {rooms_n}\n"
            f"Live result URL: {ensure_locale_curr(normalize_trip_url(final_url))}\n"
            f"Canonical search URL: {booking_url}\n"
            f"Hotel list URL: {list_url}\n"
            f"{price_note}\n"
            + (candidates_block if candidates_block else "")
            + (f"Recommended hotel name: {rec_name}\n" if rec_name else "")
            + (
                f"Recommended hotel detail link: {self.last_hotel_detail_url}\n"
                if self.last_hotel_detail_url
                else ""
            )
            + (f"{details}\n" if details else "")
            + (f"{card_block}\n" if card_block else "")
            + f"\n{content}",
            limit=9000,
        )

    def _scrape_hotel_list_card_by_id(self, hotel_id: str) -> dict[str, str]:
        """Read official name / score / price / cover from a Trip.com list card.

        Trip.com hotel *detail* URLs redirect to sign-in under Playwright, so the
        authoritative fields for the booking card come from `.list-item` on the
        hotel list (same property as the detail link's hotelId).
        """
        page = self._require_page()
        hid = re.sub(r"\D", "", str(hotel_id or ""))
        out: dict[str, str] = {}
        if not hid:
            return out
        try:
            raw = page.evaluate(
                """(hid) => {
                  const links = Array.from(
                    document.querySelectorAll('a[href*="hotelId="], a[href*="hotelid="]')
                  );
                  const hit = links.find((a) => {
                    const m = (a.getAttribute('href') || a.href || '').match(
                      /hotelId=(\\d+)/i
                    );
                    return m && m[1] === hid;
                  });
                  if (!hit) return null;
                  let root =
                    hit.closest('.list-item') ||
                    hit.closest('[class*="list-item"]') ||
                    null;
                  if (!root) {
                    let el = hit.parentElement;
                    for (let i = 0; i < 12 && el; i++) {
                      if (
                        el.querySelector &&
                        el.querySelector('.hotel-title, a.hotelName, .hotelName')
                      ) {
                        root = el;
                        break;
                      }
                      el = el.parentElement;
                    }
                  }
                  root = root || hit.parentElement || hit;
                  const pickText = (sel) => {
                    const n = root.querySelector(sel);
                    return n ? (n.innerText || '').trim().split('\\n')[0].trim() : '';
                  };
                  const name =
                    pickText('.hotel-title') ||
                    pickText('a.hotelName') ||
                    pickText('.hotelName') ||
                    pickText('[class*="hotelName"]') ||
                    '';
                  const score =
                    pickText('.comment-score .score') ||
                    pickText('.score') ||
                    pickText('.comment-score') ||
                    '';
                  const scoreLabel = pickText('.comment-desc') || '';
                  const reviews = pickText('.comment-num') || '';
                  const sale =
                    pickText('.sale.sale-blue') ||
                    pickText('.sale-blue') ||
                    pickText('.sale') ||
                    '';
                  const total = pickText('.price-highlight') || '';
                  const loc =
                    pickText('.hotel-position') ||
                    pickText('[class*="position"]') ||
                    pickText('[class*="address"]') ||
                    '';
                  let image = '';
                  const imgs = Array.from(
                    root.querySelectorAll(
                      "img.m-lazyImg__img, img[alt*='hotel overview' i], img"
                    )
                  );
                  for (const img of imgs) {
                    const src =
                      img.currentSrc ||
                      img.src ||
                      img.getAttribute('data-src') ||
                      '';
                    const alt = (img.alt || '').toLowerCase();
                    if (!src || !src.startsWith('http')) continue;
                    const low = src.toLowerCase();
                    if (!low.includes('tripcdn') && !low.includes('ak-d.tripcdn'))
                      continue;
                    if (
                      ['logo', 'icon', 'avatar', 'qrcode', 'badge'].some((x) =>
                        low.includes(x)
                      )
                    )
                      continue;
                    if (alt.includes('hotel overview') || alt.includes('overview')) {
                      image = src;
                      break;
                    }
                    if (!image) image = src;
                  }
                  const href = hit.href || hit.getAttribute('href') || '';
                  return {
                    name,
                    score: (score.match(/\\d+(?:\\.\\d+)?/) || [score])[0],
                    score_label: scoreLabel,
                    reviews,
                    price_label: sale,
                    total_label: total,
                    location: loc.replace(/\\s+/g, ' ').slice(0, 80),
                    image_url: image,
                    href,
                  };
                }""",
                hid,
            )
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            return out
        name = clean_hotel_display_name(str(raw.get("name") or ""))
        if name and 3 < len(name) < 120:
            out["name"] = name
        score = str(raw.get("score") or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", score):
            out["score"] = score
            try:
                val = float(score)
                out["score_label"] = (
                    str(raw.get("score_label") or "").strip()
                    or (
                        "Great"
                        if val >= 9
                        else "Very Good"
                        if val >= 8
                        else "Good"
                    )
                )
            except ValueError:
                out["score_label"] = str(raw.get("score_label") or "Guest rating")
        elif str(raw.get("score_label") or "").strip():
            out["score_label"] = str(raw.get("score_label")).strip()
        reviews = str(raw.get("reviews") or "").strip()
        if reviews:
            if not re.search(r"review", reviews, re.I):
                reviews = f"{reviews} reviews"
            out["reviews"] = reviews
        price = str(raw.get("price_label") or "").strip()
        if price:
            price = re.sub(r"\s+", "", price)
            if not price.upper().startswith("HK"):
                price = f"HK${price.lstrip('$')}"
            out["price_label"] = price
        total = str(raw.get("total_label") or "").strip()
        if total:
            total = re.sub(r"\s+", " ", total)
            if "HK" not in total.upper():
                total = f"HK${total.lstrip('$')}"
            out["total_label"] = total
        loc = str(raw.get("location") or "").strip()
        if loc and len(loc) > 2:
            loc = re.sub(r"(?i)\s*(show on )?map\s*$", "", loc).strip()
            loc = re.sub(r"(?i)^(.+?)(near\s+.+)$", r"\1 · \2", loc, count=1)
            out["location"] = re.sub(r"\s{2,}", " ", loc)[:80]
        img = str(raw.get("image_url") or "").strip()
        if img.startswith("http"):
            out["image_url"] = _prefer_hotel_photo_url(img)
        return out

    def _ensure_hotel_list_for_card_scrape(self) -> bool:
        """Stay on /hotels/list when possible so list-card selectors work."""
        page = self._require_page()
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        if "/hotels/list" in cur and "signin" not in cur:
            return True
        list_url = (getattr(self, "last_hotel_list_url", "") or "").strip()
        if not list_url or "trip.com" not in list_url.lower():
            return False
        try:
            self._safe_goto(list_url)
            page.wait_for_timeout(3500)
            self._dismiss_popups()
            return "/hotels/list" in ((page.url or "").lower())
        except Exception:
            return False

    def _scrape_hotel_detail_meta(self, detail_url: str) -> dict[str, str]:
        """Resolve hotel card fields for a Trip.com hotel detail URL.

        Playwright cannot open `/hotels/detail` (redirects to sign-in). Strategy:
        1. Scrape the matching `.list-item` on the hotel list (name/price/photo)
        2. HTTP-fetch the detail HTML SSR payload for official h1-equivalent
           name, score, reviews, and cover image (works without login)
        3. Only then try an in-browser detail page (logged-in / non-headless)
        """
        page = self._require_page()
        out: dict[str, str] = {}
        if not detail_url or "trip.com" not in detail_url.lower():
            return out

        from travel_agent.trip_urls import (
            _hotel_id_from_url,
            is_trusted_hotel_detail_url,
        )

        if is_trusted_hotel_detail_url(detail_url):
            self.last_hotel_detail_url = ensure_locale_curr(
                normalize_trip_url(detail_url)
            )

        hotel_id = _hotel_id_from_url(detail_url)
        if hotel_id:
            self._ensure_hotel_list_for_card_scrape()
            out = self._scrape_hotel_list_card_by_id(hotel_id)

        # Official detail-page fields (name/score/image) via HTTP — matches the
        # public page the booking URL opens in a normal browser.
        http = _http_fetch_hotel_detail_meta(detail_url)
        for k, v in http.items():
            if not v:
                continue
            if k == "name" or not out.get(k):
                out[k] = v

        if out.get("name") and (out.get("image_url") or out.get("price_label")):
            return out

        # Rare path: already on a real detail page (non-headless / logged-in)
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        on_detail = "/hotels/detail" in cur or "/hotels/v2/detail" in cur
        on_signin = "/account/signin" in cur or "/signin" in cur
        if on_signin or "/hotels/list" in cur or not on_detail:
            return out

        if is_trusted_hotel_detail_url(page.url or ""):
            self.last_hotel_detail_url = ensure_locale_curr(
                normalize_trip_url(page.url or detail_url)
            )

        name = ""
        for sel in (
            "h1",
            ".hotel-title",
            "a.hotelName",
            "[class*='hotelName' i]",
            "[class*='HotelName' i]",
            "[class*='hotel-name' i]",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count():
                    cand = (loc.inner_text(timeout=1500) or "").strip()
                    cand = cand.split("\n", 1)[0].strip()
                    if cand and 3 < len(cand) < 120 and "http" not in cand.lower():
                        name = cand
                        break
            except Exception:
                continue
        if not name:
            try:
                og = page.locator("meta[property='og:title']")
                if og.count():
                    name = (og.first.get_attribute("content") or "").strip()
            except Exception:
                name = ""
        if name:
            name = clean_hotel_display_name(name)
            if name and 3 < len(name) < 120 and not out.get("name"):
                out["name"] = name

        if not out.get("image_url"):
            try:
                og_img = page.locator("meta[property='og:image']")
                if og_img.count():
                    content = (og_img.first.get_attribute("content") or "").strip()
                    if content.startswith("http"):
                        out["image_url"] = _prefer_hotel_photo_url(content)
            except Exception:
                pass
        if not out.get("image_url"):
            try:
                overview = page.locator(
                    "img[alt*='hotel overview' i], img.m-lazyImg__img"
                ).first
                if overview.count():
                    src = (
                        overview.get_attribute("src")
                        or overview.evaluate("e => e.currentSrc || ''")
                        or ""
                    ).strip()
                    if src.startswith("http"):
                        out["image_url"] = _prefer_hotel_photo_url(src)
            except Exception:
                pass

        if not out.get("price_label"):
            try:
                sale = page.locator(".sale.sale-blue, .sale-blue").first
                if sale.count():
                    pt = (sale.inner_text(timeout=800) or "").strip()
                    if pt:
                        out["price_label"] = re.sub(r"\s+", "", pt)
            except Exception:
                pass
        if not out.get("total_label"):
            try:
                tot = page.locator(".price-highlight").first
                if tot.count():
                    tt = (tot.inner_text(timeout=800) or "").strip()
                    if tt:
                        out["total_label"] = re.sub(r"\s+", " ", tt)
            except Exception:
                pass
        if not out.get("score"):
            try:
                sc = page.locator(".comment-score .score, .score").first
                if sc.count():
                    st = (sc.inner_text(timeout=800) or "").strip()
                    m = re.search(r"\d+(?:\.\d+)?", st)
                    if m:
                        out["score"] = m.group(0)
                        val = float(out["score"])
                        out["score_label"] = (
                            "Great"
                            if val >= 9
                            else "Very Good"
                            if val >= 8
                            else "Good"
                        )
            except Exception:
                pass
        if not out.get("reviews"):
            try:
                rv = page.locator(".comment-num").first
                if rv.count():
                    rt = (rv.inner_text(timeout=800) or "").strip()
                    if rt:
                        out["reviews"] = (
                            rt if "review" in rt.lower() else f"{rt} reviews"
                        )
            except Exception:
                pass

        return out

    def _resolve_top_hotel_detail_url(
        self,
        *,
        checkin: str,
        checkout: str,
        adults: int = 2,
        rooms: int = 1,
    ) -> str:
        """Build a hotel detail URL from list-page hotelIds when anchors are sparse."""
        from travel_agent.trip_urls import (
            canonicalize_hotel_detail_url,
            is_trusted_hotel_detail_url,
        )

        page = self._require_page()
        html = ""
        try:
            html = page.content() or ""
        except Exception:
            html = ""
        # Prefer explicit hotelId values embedded in list HTML/JSON
        ids: list[str] = []
        for m in re.finditer(
            r"(?:hotelId|hotelid|masterhotelid)[\"'\s:=]+(\d{5,10})",
            html,
            re.I,
        ):
            hid = m.group(1)
            if hid not in ids:
                ids.append(hid)
            if len(ids) >= 5:
                break
        if not ids:
            # Last resort: click the first hotel card and capture navigation
            try:
                card = page.locator(
                    "a[href*='hotelId='], a[href*='hotelid='], "
                    "a[href*='/hotels/detail'], a[href*='hotel-detail-']"
                ).first
                if card.count():
                    with page.expect_navigation(timeout=12000, wait_until="domcontentloaded"):
                        card.click(timeout=5000)
                    page.wait_for_timeout(1500)
                    cur = page.url or ""
                    if is_trusted_hotel_detail_url(cur):
                        return (
                            canonicalize_hotel_detail_url(
                                cur, checkin=checkin, checkout=checkout
                            )
                            or ensure_locale_curr(normalize_trip_url(cur))
                        )
            except Exception:
                pass
            return ""

        for hid in ids:
            raw = (
                f"{settings.trip_base_url}/hotels/detail/"
                f"?hotelId={hid}&checkIn={checkin}&checkOut={checkout}"
                f"&adult={max(1, adults)}&crn={max(1, rooms)}"
                f"&locale={settings.trip_locale}&curr={settings.trip_currency}"
            )
            cand = canonicalize_hotel_detail_url(
                raw, checkin=checkin, checkout=checkout
            ) or ensure_locale_curr(raw)
            if is_trusted_hotel_detail_url(cand):
                return cand
        return ""

    @staticmethod
    def _hotel_list_url_ok(url: str) -> bool:
        low = (url or "").lower()
        if "/hotels/list" not in low:
            return False
        # Trip.com sets cityId (or city=) after a successful autocomplete pick
        return bool(re.search(r"[?&]city(?:id)?=\d+", low, re.I))

    def _patch_hotel_list_dates(
        self,
        url: str,
        checkin: str,
        checkout: str,
        adults: int = 2,
        rooms: int = 1,
    ) -> str:
        """Keep Trip.com's resolved city= from autocomplete; force our stay dates."""
        if not url or "trip.com" not in url.lower():
            return url
        from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

        parsed = urlparse(normalize_trip_url(url))
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        qs["checkin"] = checkin
        qs["checkout"] = checkout
        qs.pop("checkIn", None)
        qs.pop("checkOut", None)
        qs["adult"] = str(max(1, min(int(adults or 2), 8)))
        qs["crn"] = str(max(1, min(int(rooms or 1), 8)))
        qs.setdefault("locale", settings.trip_locale)
        qs.setdefault("curr", settings.trip_currency)
        return ensure_locale_curr(
            urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    urlencode(qs, quote_via=quote),
                    "",
                )
            )
        )

    def search_trains(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
    ) -> str:
        """Open Trip.com train search for a route."""
        page = self._require_page()
        depart_date = depart_date or _default_depart()
        params = {
            "from": origin,
            "to": destination,
            "date": depart_date,
            "locale": settings.trip_locale,
            "curr": settings.trip_currency,
        }
        # Train hub + query; site often redirects to the right market page
        url = f"{settings.trip_base_url}/trains/?{urlencode(params)}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._dismiss_popups()

        # Try filling the on-page form when present
        try:
            self._try_fill_train_form(origin, destination, depart_date)
        except Exception:
            pass

        page.wait_for_timeout(2500)
        snippet = self._extract_list_content(selectors=["main", "[class*='train']", "body"])
        return _clean_text(
            f"Train search URL: {page.url}\n"
            f"Origin: {origin} -> Destination: {destination}\n"
            f"Depart: {depart_date}\n\n{snippet}"
        )

    def search_transfers(
        self,
        location: str,
        date: str | None = None,
    ) -> str:
        """Open Trip.com airport transfers / ground transfer options."""
        page = self._require_page()
        date = date or _default_depart(21)
        params = {
            "locale": settings.trip_locale,
            "curr": settings.trip_currency,
            "keyword": location,
            "date": date,
        }
        url = f"{settings.trip_base_url}/airport-transfers/?{urlencode(params)}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._dismiss_popups()
        self._wait_for_results(
            keywords=["HK$", "transfer", "airport", "pickup", "car", "private"]
        )
        page.mouse.wheel(0, 1000)
        page.wait_for_timeout(1200)
        snippet = self._extract_list_content(
            selectors=[
                "[class*='transfer']",
                "[class*='Transfer']",
                "[class*='airport']",
                "main",
                "body",
            ]
        )
        return _clean_text(
            f"Airport transfer search URL: {page.url}\n"
            f"Location: {location}\n"
            f"Date: {date}\n\n{snippet}"
        )

    def search_cars(
        self,
        location: str,
        pickup_date: str | None = None,
        dropoff_date: str | None = None,
    ) -> str:
        """Search car rentals via Playwright on Trip.com car hire hub.

        Flow: /carhire/ → type pickup → autocomplete → Search → scrape top deal
        → keep /carrentals/detail URL for booking.
        """
        page = self._require_page()
        loc = typed_place_name(to_hotel_city(location) or location) or "Los Angeles"
        pickup_date = pickup_date or _default_depart(21)
        dropoff_date = dropoff_date or _default_return(28)

        self._update_selection_context(
            pickup_location=loc,
            pickup_date=pickup_date,
            dropoff_date=dropoff_date,
            rent_car=True,
        )

        hub = (
            f"{settings.trip_base_url}/carhire/"
            f"?channelid=14409&locale={settings.trip_locale}&curr={settings.trip_currency}"
        )

        filled = False
        for attempt in range(3):
            self._safe_goto(hub)
            page.wait_for_timeout(1600 + attempt * 400)
            self._dismiss_popups()
            filled = self._try_fill_car_form(loc, pickup_date, dropoff_date)
            if filled and self._car_results_ready(page.url or ""):
                break
            filled = False

        self._wait_for_results(
            keywords=["HK$", "HKD", "/day", "View deal", "car", "rental", "or similar"],
            attempts=16,
        )
        # Wait for structured list cards (vehicle-item-fuse) used for name/photo/price
        for _ in range(12):
            try:
                if page.locator(".vehicle-item-fuse, img.vehicle-item-fuse__image").count():
                    break
            except Exception:
                pass
            page.wait_for_timeout(700)
        for _ in range(10):
            try:
                probe = page.inner_text("body")
            except Exception:
                probe = ""
            if "HK$" in probe and ("/day" in probe.lower() or "view deal" in probe.lower()):
                break
            page.wait_for_timeout(900)
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(1500)

        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        list_url = page.url if "trip.com" in (page.url or "") else hub
        prices = [p for p in parse_prices(body) if p >= 80]
        price_note = (
            f"Parsed prices (HKD-like): {prices[:10]}"
            if prices
            else "Parsed prices: NONE — page may still be loading or blocked."
        )

        card = self._scrape_top_car_card(
            location=loc,
            pickup_date=pickup_date,
            dropoff_date=dropoff_date,
            lowest=prices[0] if prices else None,
            page_body=body,
            daily_prices=prices,
        )
        detail = (card.get("url") or "").strip()
        if not self._car_detail_url_ok(detail):
            # Prefer list-card hrefs over clicking "View deal" (click leaves the list)
            for fc in self._scrape_car_fuse_cards(limit=3):
                if self._car_detail_url_ok(fc.get("url") or ""):
                    detail = fc["url"]
                    # Fill any missing fields from the fuse card we just read
                    for k, v in fc.items():
                        if v and not card.get(k):
                            card[k] = v
                    break
            if not self._car_detail_url_ok(detail):
                detail = self._resolve_top_car_detail_url() or detail
            if detail:
                card["url"] = detail

        if self._car_detail_url_ok(card.get("url", "")):
            self.last_car_detail_url = ensure_locale_curr(
                normalize_trip_url(card["url"])
            )
            card["url"] = self.last_car_detail_url
        else:
            self.last_car_detail_url = ""

        # Always try to attach the list-page vehicle photo (search results DOM)
        if not (card.get("image_url") or "").startswith("http"):
            try:
                img = self._scrape_car_image_from_page(
                    page, car_name=card.get("name") or ""
                )
            except Exception:
                img = ""
            if img:
                card["image_url"] = img
        elif card.get("image_url"):
            card["image_url"] = (
                self._normalize_car_image_url(card["image_url"]) or card["image_url"]
            )

        self.last_car_card = dict(card)

        card_block = ""
        if card.get("name") or card.get("price_label") or card.get("url"):
            card_block = (
                "Structured car card:\n"
                f"- Car: {card.get('name', '')}\n"
                f"- Similar: {card.get('similar', '')}\n"
                f"- Vendor: {card.get('vendor', '')}\n"
                f"- Score: {card.get('score', '')}\n"
                f"- Reviews: {card.get('reviews', '')}\n"
                f"- Seats: {card.get('seats', '')}\n"
                f"- Fuel: {card.get('fuel', '')}\n"
                f"- Pickup note: {card.get('pickup_note', '')}\n"
                f"- Cancellation: {card.get('cancellation', '')}\n"
                f"- Mileage: {card.get('mileage', '')}\n"
                f"- Payment: {card.get('payment', '')}\n"
                f"- Insurance: {card.get('insurance', '')}\n"
                f"- Daily: {card.get('price_label', '')}\n"
                f"- Total: {card.get('total_label', '')}\n"
                + (
                    f"- Image: {card['image_url']}\n"
                    if card.get("image_url")
                    else ""
                )
                + f"- Location: {loc}\n"
                + f"- Pickup: {pickup_date}\n"
                + f"- Dropoff: {dropoff_date}\n"
            )

        how = (
            "Playwright carhire: typed pickup → autocomplete → Search → detail"
            if self.last_car_detail_url
            else (
                "Playwright carhire: typed pickup → Search (list only)"
                if filled
                else "Playwright carhire FAILED (check pickup location)"
            )
        )
        booking = self.last_car_detail_url or ensure_locale_curr(
            normalize_trip_url(list_url)
        )
        return _clean_text(
            f"Car search method: {how}\n"
            f"Pick-up location typed: {loc}\n"
            f"Pick-up: {pickup_date} | Drop-off: {dropoff_date}\n"
            f"Live result URL: {ensure_locale_curr(normalize_trip_url(list_url))}\n"
            f"Canonical car detail URL: {booking}\n"
            f"Car list URL: {ensure_locale_curr(normalize_trip_url(list_url))}\n"
            f"{price_note}\n"
            + (
                f"Recommended car name: {card.get('name', '')}\n"
                if card.get("name")
                else ""
            )
            + (
                f"Recommended car detail link: {self.last_car_detail_url}\n"
                if self.last_car_detail_url
                else ""
            )
            + (f"{card_block}\n" if card_block else "")
            + f"\n{body[:4500]}",
            limit=9000,
        )

    @staticmethod
    def _car_detail_url_ok(url: str) -> bool:
        low = (url or "").lower()
        return "trip.com" in low and "/carrentals/detail" in low

    @staticmethod
    def _car_results_ready(url: str) -> bool:
        low = (url or "").lower()
        if not low or "trip.com" not in low:
            return False
        return any(
            x in low
            for x in (
                "/carrentals/",
                "/carhire/",
                "list",
                "pcity=",
                "paddress=",
                "vehicle",
            )
        )

    def _try_fill_car_form(
        self,
        location: str,
        pickup_date: str,
        dropoff_date: str,
    ) -> bool:
        """Type pickup into Trip.com car hire hub and Search."""
        page = self._require_page()
        location = typed_place_name(location)
        if not location:
            return False
        try:
            location_box = page.locator(
                "input[placeholder*='Pick' i], "
                "input[placeholder*='pick' i], "
                "input[placeholder*='Location' i], "
                "input[placeholder*='City' i], "
                "input[placeholder*='Where' i], "
                "input[aria-label*='Pick' i], "
                "input[aria-label*='location' i], "
                "input[aria-label*='Where' i]"
            ).first
            if not location_box.count():
                location_box = page.locator(
                    "form input[type='text'], [class*='search'] input[type='text']"
                ).first
            if not location_box.count():
                return False

            location_box.click(timeout=8000)
            page.wait_for_timeout(250)
            try:
                location_box.fill("")
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            # Type spaces as spaces — never '+' between words
            try:
                location_box.type(location, delay=45)
            except Exception:
                location_box.fill(location)
            page.wait_for_timeout(1400)

            picked = False
            loc_l = location.lower()
            try:
                suggestions = page.locator(
                    "[class*='suggest'] li, "
                    "[class*='Suggest'] li, "
                    "[class*='autocomplete'] li, "
                    "[role='option'], "
                    "[class*='dropdown'] li, "
                    "ul[class*='list'] li"
                )
                n = min(suggestions.count(), 12)
                for i in range(n):
                    try:
                        el = suggestions.nth(i)
                        if not el.is_visible(timeout=350):
                            continue
                        text = (el.inner_text(timeout=400) or "").strip()
                        if not text:
                            continue
                        low = text.lower()
                        if loc_l in low or loc_l.split()[0] in low:
                            el.click(timeout=3000)
                            picked = True
                            break
                    except Exception:
                        continue
                if not picked and n > 0:
                    for i in range(n):
                        try:
                            el = suggestions.nth(i)
                            if el.is_visible(timeout=300):
                                el.click(timeout=3000)
                                picked = True
                                break
                        except Exception:
                            continue
            except Exception:
                picked = False

            if not picked:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(200)
                page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            _ = (pickup_date, dropoff_date)  # hub often keeps defaults / calendar UI

            search_btn = page.get_by_role(
                "button", name=re.compile(r"search|find cars|show cars", re.I)
            )
            if search_btn.count():
                search_btn.first.click(timeout=8000)
            else:
                btn = page.locator(
                    "button:has-text('Search'), button[type='submit']"
                ).first
                if btn.count():
                    btn.click(timeout=8000)
                else:
                    location_box.press("Enter")

            try:
                page.wait_for_url(
                    re.compile(r"car(hire|rentals)|pcity=|list", re.I), timeout=20000
                )
            except Exception:
                page.wait_for_timeout(4500)
            for _ in range(8):
                try:
                    probe = page.inner_text("body")
                except Exception:
                    probe = ""
                if any(p >= 80 for p in parse_prices(probe)):
                    break
                if re.search(r"view deal|/day", probe, re.I):
                    break
                page.wait_for_timeout(700)
            return True
        except Exception:
            return False

    def _extract_car_detail_options(self, limit: int = 8) -> list[tuple[str, str]]:
        """Return (detail_url, car_label) pairs from the car hire list page."""
        page = self._require_page()
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        try:
            anchors = page.locator("a[href*='/carrentals/detail']")
            count = min(anchors.count(), 40)
        except Exception:
            return found

        for i in range(count):
            try:
                a = anchors.nth(i)
                href = (a.get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href:
                continue
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            href = ensure_locale_curr(normalize_trip_url(href))
            if not self._car_detail_url_ok(href) or href in seen:
                continue
            label = ""
            try:
                raw_label = (a.inner_text(timeout=500) or "").strip()
                raw_label = re.sub(r"\s+", " ", raw_label)
                if (
                    raw_label
                    and 3 < len(raw_label) < 90
                    and "http" not in raw_label.lower()
                    and not re.fullmatch(
                        r"(?i)(view deal|book|select|see details?|check availability|>)+",
                        raw_label,
                    )
                ):
                    label = raw_label
            except Exception:
                label = ""
            seen.add(href)
            found.append((href, label))
            if len(found) >= limit:
                break
        return found

    def _resolve_top_car_detail_url(self) -> str:
        """Find or open the top car deal detail URL."""
        page = self._require_page()
        # Prefer existing detail anchors on the list
        try:
            anchors = page.locator("a[href*='/carrentals/detail']")
            count = min(anchors.count(), 20)
        except Exception:
            count = 0
        for i in range(count):
            try:
                href = (anchors.nth(i).get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href:
                continue
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            href = ensure_locale_curr(normalize_trip_url(href))
            if self._car_detail_url_ok(href):
                return href

        # Click View deal and capture navigation
        try:
            btn = page.get_by_role("button", name=re.compile(r"view deal", re.I))
            if not btn.count():
                btn = page.locator(
                    "a:has-text('View deal'), button:has-text('View deal'), "
                    "[class*='deal']:has-text('View')"
                )
            if btn.count():
                with page.expect_navigation(
                    timeout=15000, wait_until="domcontentloaded"
                ):
                    btn.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                cur = page.url or ""
                if self._car_detail_url_ok(cur):
                    return ensure_locale_curr(normalize_trip_url(cur))
        except Exception:
            pass
        return ""

    def _scrape_top_car_card(
        self,
        *,
        location: str = "",
        pickup_date: str = "",
        dropoff_date: str = "",
        lowest: float | None = None,
        page_body: str = "",
        daily_prices: list[float] | None = None,
    ) -> dict[str, str]:
        """Parse car rental deals and LLM-pick the best from scraped candidates."""
        page = self._require_page()
        try:
            body = page_body or page.inner_text("body")
        except Exception:
            body = page_body or ""

        # Primary source: structured .vehicle-item-fuse list cards (name/price/photo)
        fuse_cards = self._scrape_car_fuse_cards(limit=10)
        for row in fuse_cards:
            row.setdefault("location", location)
            row.setdefault("pickup_date", pickup_date)
            row.setdefault("dropoff_date", dropoff_date)

        candidates = list(fuse_cards)
        if len(candidates) < 2:
            # Merge text/LLM candidates when fuse DOM is sparse
            extra = self._collect_car_candidates(
                body,
                location=location,
                pickup_date=pickup_date,
                dropoff_date=dropoff_date,
                prices=daily_prices or ([lowest] if lowest is not None else None),
            )
            for row in extra:
                if fuse_cards and not row.get("image_url"):
                    want = (row.get("name") or "").lower()
                    for fc in fuse_cards:
                        fname = (fc.get("name") or "").lower()
                        if want and (
                            want in fname
                            or fname in want
                            or want.split(" or ")[0].strip() in fname
                        ):
                            for k, v in fc.items():
                                if v and not row.get(k):
                                    row[k] = v
                            break
                    if not row.get("image_url") and fuse_cards:
                        row["image_url"] = fuse_cards[0].get("image_url") or ""
                candidates.append(row)

        # Deduplicate by name+price
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in candidates:
            key = (
                (row.get("name") or "").lower(),
                row.get("price_label") or "",
            )
            if key in seen and key != ("", ""):
                continue
            seen.add(key)
            deduped.append(row)
        candidates = deduped

        def _finalize(card: dict[str, str]) -> dict[str, str]:
            card.setdefault("price_unit", "/day")
            card.setdefault("location", location)
            card.setdefault("pickup_date", pickup_date)
            card.setdefault("dropoff_date", dropoff_date)
            if lowest is not None and not card.get("price_label"):
                card["price_label"] = f"HK${lowest:,.0f}"
            if card.get("image_url"):
                card["image_url"] = (
                    self._normalize_car_image_url(card["image_url"])
                    or card["image_url"]
                )
            if not card.get("image_url"):
                card["image_url"] = self._scrape_car_image_from_page(
                    page, car_name=card.get("name") or ""
                )
            if not card.get("name") or card["name"].lower().startswith(
                ("car rental in ", "recommended car", "searching")
            ):
                # Prefer a real fuse/list title over the location stub
                for fc in fuse_cards:
                    if fc.get("name") and not fc["name"].lower().startswith(
                        "car rental"
                    ):
                        card["name"] = fc["name"]
                        if fc.get("similar"):
                            card["similar"] = fc["similar"]
                        break
            if not card.get("name"):
                card["name"] = (
                    f"Car rental in {location}" if location else "Recommended car"
                )
            return card

        if len(candidates) > 1:
            picked = self._pick_car_candidate(candidates)
            if picked:
                return _finalize(dict(picked))
        if candidates:
            return _finalize(dict(candidates[0]))

        card: dict[str, str] = {
            "name": "",
            "similar": "",
            "vendor": "",
            "score": "",
            "reviews": "",
            "seats": "",
            "fuel": "",
            "pickup_note": "",
            "cancellation": "",
            "mileage": "",
            "payment": "",
            "insurance": "",
            "price_label": "",
            "price_unit": "/day",
            "total_label": "",
            "image_url": "",
            "url": "",
            "location": location,
            "pickup_date": pickup_date,
            "dropoff_date": dropoff_date,
        }

        # Detail link on page
        for u in self._extract_detail_links(kinds=("carrentals", "car"), limit=8):
            if self._car_detail_url_ok(u):
                card["url"] = ensure_locale_curr(normalize_trip_url(u))
                break
        if not card["url"]:
            try:
                a = page.locator("a[href*='/carrentals/detail']").first
                if a.count():
                    href = (a.get_attribute("href") or "").strip()
                    if href.startswith("/"):
                        href = f"{settings.trip_base_url.rstrip('/')}{href}"
                    if self._car_detail_url_ok(href):
                        card["url"] = ensure_locale_curr(normalize_trip_url(href))
            except Exception:
                pass

        parsed = _parse_car_chunk(body[:14000])
        for k, v in parsed.items():
            if v and not card.get(k):
                card[k] = v
        if lowest is not None and not card.get("price_label"):
            card["price_label"] = f"HK${lowest:,.0f}"
        return _finalize(card)

    def _scrape_car_fuse_cards(self, *, limit: int = 8) -> list[dict[str, str]]:
        """Read Trip.com list cards (``.vehicle-item-fuse``) with photo + deal fields.

        Matches the public search-results DOM (name, seats/bags/Automatic, vendor,
        price, View deal link, ``img.vehicle-item-fuse__image``).
        """
        page = self._require_page()
        try:
            rows = page.evaluate(
                """(limit) => {
                  const roots = Array.from(
                    document.querySelectorAll(
                      '.vehicle-item-fuse, [class*="vehicle-item-fuse"]'
                    )
                  );
                  const out = [];
                  const seen = new Set();
                  for (const card of roots) {
                    const img =
                      card.querySelector('img.vehicle-item-fuse__image') ||
                      card.querySelector('img[class*="vehicle-item-fuse__image"]') ||
                      card.querySelector('img[class*="vehicle-item"]') ||
                      card.querySelector('img');
                    let src = '';
                    if (img) {
                      src =
                        img.currentSrc ||
                        img.src ||
                        img.getAttribute('data-src') ||
                        img.getAttribute('src') ||
                        '';
                    }
                    let href = '';
                    const links = Array.from(
                      card.querySelectorAll(
                        "a[href*='carrentals/detail'], a[href*='/carrentals/'], a[href*='View']"
                      )
                    );
                    for (const a of links) {
                      const h = a.href || a.getAttribute('href') || '';
                      if (h && h.includes('detail')) {
                        href = h;
                        break;
                      }
                      if (!href && h) href = h;
                    }
                    // Some deals store the detail URL on a button/data attr
                    if (!href) {
                      const any = card.querySelector('[href*="carrentals/detail"]');
                      if (any) href = any.href || any.getAttribute('href') || '';
                    }
                    const text = (card.innerText || '')
                      .replace(/\\u00a0/g, ' ')
                      .replace(/[ \\t]+/g, ' ')
                      .trim();
                    if (!text || text.length < 20) continue;
                    const key = text.slice(0, 80) + '|' + (src || '').slice(-40);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({ image_url: src, url: href, text: text.slice(0, 900) });
                    if (out.length >= limit) break;
                  }
                  return out;
                }""",
                limit,
            )
        except Exception:
            rows = []

        cleaned: list[dict[str, str]] = []
        for row in rows or []:
            text = str(row.get("text") or "")
            parsed = _parse_car_chunk(text)
            img = self._normalize_car_image_url(str(row.get("image_url") or ""))
            href = str(row.get("url") or "").strip()
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            if href and "trip.com" in href.lower():
                href = ensure_locale_curr(normalize_trip_url(href))
                if self._car_detail_url_ok(href):
                    parsed["url"] = href
            if img:
                parsed["image_url"] = img
            # Prefer full "Name or similar …" as display name
            full = re.search(
                r"(?i)([A-Z][A-Za-z0-9 \-]+?\s+or similar\s+[A-Za-z ]+)",
                text,
            )
            if full:
                whole = re.sub(r"\s+", " ", full.group(1)).strip()
                # Split name / similar for the card layout
                parts = re.match(
                    r"(?i)(.+?)\s+(or similar\s+.+)$",
                    whole,
                )
                if parts:
                    parsed["name"] = parts.group(1).strip()
                    parsed["similar"] = parts.group(2).strip()
                else:
                    parsed["name"] = whole
            if parsed.get("name") or parsed.get("price_label") or img:
                cleaned.append(parsed)
        return cleaned

    @staticmethod
    def _normalize_car_image_url(src: str) -> str:
        """Keep Trip.com / Ctrip CDN vehicle photos usable for the GUI card."""
        out = (src or "").strip()
        if not out or not out.startswith("http"):
            return ""
        low = out.lower()
        if any(
            x in low
            for x in ("logo", "icon", "avatar", "qrcode", "badge", "sprite", "pixel")
        ):
            return ""
        # Prefer CDN hosts used by Trip.com car hire list cards
        if not any(
            h in low
            for h in (
                "c-ctrip.com",
                "tripcdn.com",
                "ak-d.tripcdn",
                "dimg04.",
                "dimg.",
            )
        ):
            # Still allow other https car photos if clearly from trip
            if "trip.com" not in low and "ctrip" not in low:
                return ""
        # Prefer a slightly larger resize when Trip.com uses proc=resize
        out = re.sub(
            r"([?&]proc=resize/[^&]*w_)\d+",
            r"\g<1>600",
            out,
            flags=re.I,
        )
        out = re.sub(
            r"([?&]proc=resize/[^&]*h_)\d+",
            r"\g<1>400",
            out,
            flags=re.I,
        )
        return out

    def _scrape_car_image_from_page(self, page: Page, *, car_name: str = "") -> str:
        """Cover photo from Trip.com car hire list (``.vehicle-item-fuse__image``)."""
        # 1) Structured list cards — matches the search-page DOM the user inspected
        fuse = self._scrape_car_fuse_cards(limit=10)
        want = re.sub(r"\s+", " ", (car_name or "").strip()).lower()
        if want:
            for row in fuse:
                name = (row.get("name") or "").lower()
                if want in name or name in want or want.split(" or ")[0] in name:
                    if row.get("image_url"):
                        return row["image_url"]
        for row in fuse:
            if row.get("image_url"):
                return row["image_url"]

        # 2) Direct selector used on hk.trip.com results
        try:
            loc = page.locator("img.vehicle-item-fuse__image")
            count = min(loc.count(), 8)
        except Exception:
            count = 0
        for i in range(count):
            try:
                src = (
                    loc.nth(i).get_attribute("src")
                    or loc.nth(i).evaluate("e => e.currentSrc || e.src || ''")
                    or ""
                ).strip()
            except Exception:
                continue
            norm = self._normalize_car_image_url(src)
            if norm:
                return norm

        # 3) Fallback: any CDN img that looks like a vehicle photo
        try:
            imgs = page.evaluate(
                """() => Array.from(document.querySelectorAll('img')).map(e => ({
                  src: e.currentSrc || e.src || e.getAttribute('data-src') || '',
                  alt: e.alt || '',
                  cls: (e.className || '').toString(),
                  w: e.naturalWidth || e.width || 0,
                  h: e.naturalHeight || e.height || 0
                })).filter(x => x.src && x.src.startsWith('http'))"""
            )
        except Exception:
            imgs = []
        best = ""
        best_score = -1
        for im in imgs or []:
            src = self._normalize_car_image_url(str(im.get("src") or ""))
            if not src:
                continue
            low = src.lower()
            cls = str(im.get("cls") or "").lower()
            alt = str(im.get("alt") or "").lower()
            score = 0
            if "vehicle-item-fuse__image" in cls:
                score += 100
            if "vehicle" in cls or "vehicle" in alt or "car" in alt:
                score += 40
            if "c-ctrip.com" in low or "dimg" in low:
                score += 30
            if "tripcdn" in low:
                score += 20
            area = int(im.get("w") or 0) * int(im.get("h") or 0)
            if area >= 8_000:
                score += 10
            if score > best_score:
                best_score = score
                best = src
        return best

    def browse_url(self, url: str) -> str:
        """Navigate to any Trip.com (or related) URL and summarize visible content."""
        page = self._require_page()
        if not url.startswith("http"):
            url = f"{settings.trip_base_url.rstrip('/')}/{url.lstrip('/')}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._dismiss_popups()
        return self.get_page_summary()

    def compare_flight_prices(
        self,
        origin: str,
        destination: str,
        dates: str | list[str] | None = None,
        trip_type: str = "oneway",
        return_date: str | None = None,
        adults: int = 1,
        alternate_destinations: str | list[str] | None = None,
    ) -> str:
        """Compare flight prices across dates and/or destinations on Trip.com."""
        date_list = _as_list(dates) or [
            _default_depart(14),
            _default_depart(21),
            _default_depart(28),
        ]
        dest_list = [destination] + _as_list(alternate_destinations)
        # Cap work so one tool call stays responsive
        date_list = date_list[:4]
        dest_list = dest_list[:3]

        rows: list[dict[str, Any]] = []
        raw_blocks: list[str] = []
        for dest in dest_list:
            for d in date_list:
                label = f"{origin.upper()}->{dest.upper()} depart {d}"
                if trip_type.strip().lower() in {"round", "roundtrip", "rt", "2"}:
                    label += f" return {return_date or _default_return()}"
                try:
                    page_text = self.search_flights(
                        origin=origin,
                        destination=dest,
                        depart_date=d,
                        return_date=return_date,
                        trip_type=trip_type,
                        adults=adults,
                        include_return_leg=False,
                    )
                    url = self._require_page().url
                    summary = summarize_prices(label, page_text, url=url)
                    rows.append(summary)
                    raw_blocks.append(
                        f"--- {label} ---\n"
                        f"Lowest: {summary['lowest_hkd']} | "
                        f"Prices: {summary['prices_hkd'][:8]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    rows.append(
                        {
                            "label": label,
                            "url": "",
                            "price_count": 0,
                            "prices_hkd": [],
                            "lowest_hkd": None,
                            "highest_hkd": None,
                            "median_hkd": None,
                            "error": str(exc),
                        }
                    )
                    raw_blocks.append(f"--- {label} ---\nERROR: {exc}")

        table = format_comparison_table(rows, "FLIGHT PRICE COMPARISON (HKD)")
        return _clean_text(
            table
            + "\n\nNotes: Prices parsed from Trip.com Hong Kong page text. "
            "Use the cheapest labeled option as the primary recommendation.\n\n"
            + "\n".join(raw_blocks),
            limit=9000,
        )

    def compare_hotel_prices(
        self,
        city: str,
        checkin: str | None = None,
        checkout: str | None = None,
        adults: int = 2,
        rooms: int = 1,
        alternate_cities: str | list[str] | None = None,
        alternate_checkins: str | list[str] | None = None,
    ) -> str:
        """Compare hotel prices across cities and/or check-in dates."""
        checkin = checkin or _default_depart(14)
        checkout = checkout or _default_return(17)
        cities = [city] + _as_list(alternate_cities)
        checkins = _as_list(alternate_checkins) or [checkin]
        cities = cities[:3]
        checkins = checkins[:3]

        # Keep stay length when shifting check-in
        stay_nights = nights_between(checkin, checkout)

        rows: list[dict[str, Any]] = []
        raw_blocks: list[str] = []
        for c in cities:
            for cin in checkins:
                try:
                    cout = (
                        date.fromisoformat(cin) + timedelta(days=stay_nights)
                    ).isoformat()
                except ValueError:
                    cout = checkout
                label = f"{c} | {cin} -> {cout} ({stay_nights}n)"
                try:
                    page_text = self.search_hotels(
                        city=c,
                        checkin=cin,
                        checkout=cout,
                        adults=adults,
                        rooms=rooms,
                    )
                    url = self._require_page().url
                    summary = summarize_prices(label, page_text, url=url)
                    if summary["lowest_hkd"] is not None:
                        summary["est_total_lowest_hkd"] = round(
                            summary["lowest_hkd"] * stay_nights, 2
                        )
                    rows.append(summary)
                    raw_blocks.append(
                        f"--- {label} ---\n"
                        f"Lowest/night: {summary['lowest_hkd']} | "
                        f"Est total: {summary.get('est_total_lowest_hkd')} | "
                        f"Prices: {summary['prices_hkd'][:8]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    rows.append(
                        {
                            "label": label,
                            "url": "",
                            "price_count": 0,
                            "prices_hkd": [],
                            "lowest_hkd": None,
                            "highest_hkd": None,
                            "median_hkd": None,
                            "error": str(exc),
                        }
                    )
                    raw_blocks.append(f"--- {label} ---\nERROR: {exc}")

        table = format_comparison_table(rows, "HOTEL PRICE COMPARISON (HKD / night-ish)")
        return _clean_text(
            table
            + "\n\nNotes: Values are parsed from visible Trip.com Hong Kong hotel listings. "
            "Treat as approximate nightly rates unless the page shows totals.\n\n"
            + "\n".join(raw_blocks),
            limit=9000,
        )

    def propose_trip_route(
        self,
        destination: str,
        nights: int | str = 7,
        depart_date: str | None = None,
        origin: str = "Hong Kong",
        arrive_city: str = "",
        return_city: str = "",
        stay_cities: str = "",
        interests: str = "",
    ) -> str:
        """Rough route only — arrive/return hubs + stay cities. No Trip.com scrape yet."""
        from travel_agent.regions import format_route_proposal, propose_trip_route

        try:
            n = max(1, int(nights or 0))
        except (TypeError, ValueError):
            n = 0
        # Prefer the trip length from the GUI / plan context when the model
        # undershoots (e.g. proposes 8 nights for a 14-night trip).
        ctx_n = int(getattr(self.selection_context, "nights", 0) or 0)
        if ctx_n > 0 and (n <= 0 or n < ctx_n):
            n = ctx_n
        if n <= 0:
            n = 7
        depart_date = depart_date or _default_depart(21)
        route = propose_trip_route(
            destination,
            n,
            depart_date=depart_date,
            arrive_city=arrive_city or "",
            return_city=return_city or "",
            stay_cities=stay_cities or "",
            interests=interests or "",
        )
        self.last_proposed_route = route
        text = format_route_proposal(route, origin=origin or "Hong Kong")
        return text

    def search_open_jaw_flights(
        self,
        *,
        origin: str,
        arrive_airport: str,
        return_airport: str,
        depart_date: str,
        return_date: str,
        adults: int = 1,
    ) -> dict[str, str]:
        """Open two Trip.com one-way searches; pick top fare on each; fill card.

        Returns a merged card with:
          - outbound fields + ``flight`` (outbound search URL)
          - return_* fields + ``flight_return`` (return search URL)
        """
        origin_code = to_flight_code(origin).upper()
        arrive_code = to_flight_code(arrive_airport).upper() or arrive_airport.upper()
        return_from = to_flight_code(return_airport).upper() or return_airport.upper()
        adults_n = max(1, min(int(adults or 1), 9))

        # Always emit both search-page links (even if scrape is slow/empty)
        out_url = build_flight_search_url(
            origin=origin_code,
            destination=arrive_code,
            depart_date=depart_date,
            trip_type="oneway",
            adults=adults_n,
        )
        ret_url = build_flight_search_url(
            origin=return_from,
            destination=origin_code,
            depart_date=return_date,
            trip_type="oneway",
            adults=adults_n,
        )

        out_text = self.search_flights(
            origin=origin_code,
            destination=arrive_code,
            depart_date=depart_date,
            trip_type="oneway",
            adults=adults_n,
            include_return_leg=False,
        )
        out_snap = dict(self.last_flight_card or {})
        out_fields = extract_booking_urls(out_text)
        for k, v in out_snap.items():
            if v and (not out_fields.get(k) or k in {
                "flight_depart", "flight_arrive", "flight_airline", "flight_price",
            }):
                out_fields[k] = v
        if out_fields.get("flight"):
            out_url = out_fields["flight"]

        ret_text = self.search_flights(
            origin=return_from,
            destination=origin_code,
            depart_date=return_date,
            trip_type="oneway",
            adults=adults_n,
            include_return_leg=False,
        )
        ret_snap = dict(self.last_flight_card or {})
        ret_fields = extract_booking_urls(ret_text)
        for k, v in ret_snap.items():
            if v and (not ret_fields.get(k) or k in {
                "flight_depart", "flight_arrive", "flight_airline", "flight_price",
            }):
                ret_fields[k] = v
        if ret_fields.get("flight"):
            ret_url = ret_fields["flight"]

        # Retry outbound once if the first page didn't yield times
        if not out_fields.get("flight_depart"):
            try:
                self._require_page().wait_for_timeout(2500)
                out_text = self.search_flights(
                    origin=origin_code,
                    destination=arrive_code,
                    depart_date=depart_date,
                    trip_type="oneway",
                    adults=adults_n,
                    include_return_leg=False,
                )
                out_snap = dict(self.last_flight_card or {})
                out_fields = extract_booking_urls(out_text)
                for k, v in out_snap.items():
                    if v:
                        out_fields[k] = v
                if out_fields.get("flight"):
                    out_url = out_fields["flight"]
            except Exception:
                pass

        card: dict[str, str] = {
            "flight": out_url,
            "flight_return": ret_url,
            "flight_from": origin_code,
            "flight_to": arrive_code,
            "flight_return_from": return_from,
            "flight_return_to": origin_code,
            "flight_date": depart_date,
            "flight_return_date": return_date,
            "flight_airline": out_fields.get("flight_airline", ""),
            "flight_airline_logo": out_fields.get("flight_airline_logo", ""),
            "flight_depart": out_fields.get("flight_depart", ""),
            "flight_arrive": out_fields.get("flight_arrive", ""),
            "flight_duration": out_fields.get("flight_duration", ""),
            "flight_stops": out_fields.get("flight_stops", "") or "Direct",
            "flight_return_airline": ret_fields.get("flight_airline", ""),
            "flight_return_airline_logo": ret_fields.get("flight_airline_logo", ""),
            "flight_return_depart": ret_fields.get("flight_depart", ""),
            "flight_return_arrive": ret_fields.get("flight_arrive", ""),
            "flight_return_duration": ret_fields.get("flight_duration", ""),
            "flight_return_stops": ret_fields.get("flight_stops", "") or "Direct",
        }

        out_p = summarize_prices("outbound", out_text).get("lowest_hkd")
        ret_p = summarize_prices("return", ret_text).get("lowest_hkd")
        if out_p is None and out_fields.get("flight_price"):
            try:
                out_p = float(re.sub(r"[^\d.]", "", out_fields["flight_price"]) or "0") or None
            except ValueError:
                out_p = None
        if ret_p is None and ret_fields.get("flight_price"):
            try:
                ret_p = float(re.sub(r"[^\d.]", "", ret_fields["flight_price"]) or "0") or None
            except ValueError:
                ret_p = None
        if out_p is not None and ret_p is not None:
            card["flight_price"] = _fmt_hkd(round(float(out_p) + float(ret_p), 2))
            card["flight_outbound_price"] = _fmt_hkd(out_p)
            card["flight_return_price"] = _fmt_hkd(ret_p)
        elif out_p is not None:
            card["flight_price"] = _fmt_hkd(out_p)
            card["flight_outbound_price"] = card["flight_price"]
        elif ret_p is not None:
            card["flight_price"] = _fmt_hkd(ret_p)
            card["flight_return_price"] = card["flight_price"]
        elif out_fields.get("flight_price"):
            card["flight_price"] = out_fields["flight_price"]

        self.last_flight_card = {k: v for k, v in card.items() if v}
        self.last_plan_flight_card = dict(self.last_flight_card)
        self._last_open_jaw_raw = (out_text, ret_text)
        return dict(self.last_plan_flight_card)

    def plan_trip(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
        return_date: str | None = None,
        adults: int = 2,
        hotel_city: str | None = None,
        budget_hkd: float | None = None,
        interests: str = "",
        rent_car: bool | str | None = None,
        include_flights: bool | str | None = True,
        include_trains: bool | str | None = True,
        include_transfers: bool | str | None = True,
        arrive_airport: str | None = None,
        return_airport: str | None = None,
        stay_cities: str | None = None,
    ) -> str:
        """Build a trip plan comparing transport modes + hotels on Trip.com."""
        depart_date = depart_date or _default_depart(21)
        return_date = return_date or _default_return_from(depart_date, 7)
        hotel_city = typed_place_name(to_hotel_city(hotel_city or destination))

        # GUI / selection_context is the source of truth for trip length.
        # The model often passes a default ~7-night return even when the user
        # asked for 14 nights — never shorten hotels below that.
        ctx_n = int(getattr(self.selection_context, "nights", 0) or 0)
        ctx_in = (getattr(self.selection_context, "checkin", "") or "").strip()
        ctx_out = (getattr(self.selection_context, "checkout", "") or "").strip()
        if ctx_in:
            depart_date = ctx_in
        if ctx_out:
            return_date = ctx_out
        nights = max(1, nights_between(depart_date, return_date))
        if ctx_n > nights:
            nights = ctx_n
            return_date = ctx_out or _default_return_from(depart_date, nights)
        elif ctx_n > 0:
            nights = max(nights, ctx_n)
            if nights_between(depart_date, return_date) < nights:
                return_date = ctx_out or _default_return_from(depart_date, nights)

        # Prefer explicit rent_car / GUI checkbox; do not infer from travel style
        if rent_car is None or (isinstance(rent_car, str) and not str(rent_car).strip()):
            need_car = bool(getattr(self.selection_context, "rent_car", False))
        else:
            need_car = _as_bool(rent_car, default=False)
        want_flights = _as_bool(include_flights, default=True)
        want_trains = _as_bool(include_trains, default=True)
        want_transfers = _as_bool(include_transfers, default=True)
        if not want_flights and not want_trains:
            want_flights = True

        self._update_selection_context(
            origin=origin or "Hong Kong",
            destination=destination or hotel_city,
            nights=nights,
            budget_hkd=budget_hkd,
            interests=interests or "",
            travel_styles=interests or "",
            checkin=depart_date,
            checkout=return_date,
            adults=adults,
            rent_car=need_car,
            pickup_date=depart_date,
            dropoff_date=return_date,
        )

        from travel_agent.regions import (
            align_route_to_nights,
            build_regional_route,
            format_route_proposal,
            is_regional_destination,
            propose_trip_route,
        )

        # Prefer explicit route args, then last propose_trip_route, then auto-propose
        regional = None
        if arrive_airport or return_airport or stay_cities:
            regional = propose_trip_route(
                destination,
                nights,
                depart_date=depart_date,
                arrive_city=arrive_airport or "",
                return_city=return_airport or "",
                stay_cities=stay_cities or "",
                interests=interests or "",
            )
        elif self.last_proposed_route is not None:
            regional = self.last_proposed_route
        else:
            regional = propose_trip_route(
                destination,
                nights,
                depart_date=depart_date,
                interests=interests or "",
            )

        # Hotels must cover the full trip — never keep a shorter proposed split
        if regional is not None:
            stay_total = sum(max(1, int(s.nights or 1)) for s in regional.stays)
            if stay_total != nights:
                rebuilt = None
                if is_regional_destination(destination):
                    rebuilt = build_regional_route(
                        destination, nights, depart_date=depart_date
                    )
                if rebuilt is not None:
                    rebuilt.arrive_airport = (
                        regional.arrive_airport or rebuilt.arrive_airport
                    )
                    rebuilt.depart_airport = (
                        regional.depart_airport or rebuilt.depart_airport
                    )
                    regional = rebuilt
                else:
                    regional = align_route_to_nights(
                        regional, nights, depart_date=depart_date or ""
                    )
            else:
                regional = align_route_to_nights(
                    regional, nights, depart_date=depart_date or ""
                )
            self.last_proposed_route = regional

        route_preamble = format_route_proposal(
            regional, origin=origin or "Hong Kong"
        )

        arrive_code = to_flight_code(destination)
        return_from_code = arrive_code
        if regional:
            arrive_code = regional.arrive_airport
            return_from_code = regional.depart_airport
            hotel_city = regional.stays[0].city if regional.stays else hotel_city

        # Guard: never search Trip.com with placeholder / invalid IATA (shows as XXX/--:--/n/a).
        if not arrive_code or str(arrive_code).lower() in {"xxx", "na", "n/a"}:
            arrive_code = to_flight_code(destination) or to_flight_code(hotel_city or "")
        if not return_from_code or str(return_from_code).lower() in {"xxx", "na", "n/a"}:
            return_from_code = arrive_code
        if regional and arrive_code:
            regional.arrive_airport = str(arrive_code).lower()
            regional.depart_airport = str(return_from_code or arrive_code).lower()
            if regional.stays and not to_flight_code(regional.stays[0].city):
                regional.stays[0].airport = str(arrive_code).lower()
                if "," in (regional.stays[0].city or ""):
                    regional.stays[0].city = regional.stays[0].city.split(",", 1)[0].strip()
            self.last_proposed_route = regional

        # Treat multi-city / open-jaw as regional for flight+hotel scraping
        open_jaw_route = bool(
            regional
            and arrive_code
            and return_from_code
            and arrive_code.upper() != return_from_code.upper()
        )
        multi_stay = bool(regional and len(regional.stays) > 1)

        # Flights first (before attractions/hotels) so fare pages are not blocked
        # after heavy things-to-do scraping — empty scrapes show as --:-- in the GUI.
        attractions: list[str] = []

        raw_blocks: list[str] = []
        sections: list[str] = []
        booking_lines: list[str] = []
        transport_lows: list[tuple[str, float]] = []
        n = 1

        flight_url = ""
        flight_low = None
        flight_compare_text = ""
        flight_best_label = ""
        flight_snippets: list[str] = []
        flight_prices: dict[str, Any] = {}
        recommended_flight_url = ""
        recommended_flight_date = depart_date
        recommended_flight_price = None
        recommended_flight_option = ""
        flight_card_fields: dict[str, str] = {}
        structured_flight_card = ""
        if want_flights:
            # Use the user's dates only — multi-date compares multiply scrape time
            # (each Trip.com search is slow with hub form / fare loads).
            open_jaw = bool(
                (open_jaw_route or multi_stay)
                and arrive_code.upper() != return_from_code.upper()
            )
            if open_jaw:
                # Outbound to arrive city + separate return from depart city
                out_text = self.search_flights(
                    origin=origin,
                    destination=arrive_code,
                    depart_date=depart_date,
                    trip_type="oneway",
                    adults=adults,
                    include_return_leg=False,
                )
                # Preserve outbound scrape BEFORE return search overwrites last_flight_card
                out_card_snap = dict(self.last_flight_card or {})
                out_fields = extract_booking_urls(out_text)
                for k, v in out_card_snap.items():
                    if v and not out_fields.get(k):
                        out_fields[k] = v

                ret_text = self.search_flights(
                    origin=return_from_code,
                    destination=origin,
                    depart_date=return_date,
                    trip_type="oneway",
                    adults=adults,
                    include_return_leg=False,
                )
                ret_card_snap = dict(self.last_flight_card or {})
                ret_fields = extract_booking_urls(ret_text)
                for k, v in ret_card_snap.items():
                    if v and not ret_fields.get(k):
                        ret_fields[k] = v

                flight_text = (
                    f"OPEN-JAW REGIONAL FLIGHTS ({regional.label if regional else destination})\n"
                    f"Outbound: {origin.upper()} → {arrive_code.upper()}\n"
                    f"{out_text}\n\n"
                    f"Return: {return_from_code.upper()} → {origin.upper()}\n"
                    f"{ret_text}"
                )
                recommended_flight_date = depart_date
                recommended_flight_url = (
                    out_fields.get("flight")
                    or out_card_snap.get("flight")
                    or ""
                )
                flight_compare_text = (
                    f"Open-jaw on requested dates "
                    f"(outbound {depart_date}, return {return_date})"
                )
                flight_best_label = flight_compare_text
                # Combine prices when both legs report HKD
                out_p = summarize_prices("outbound", out_text).get("lowest_hkd")
                ret_p = summarize_prices("return", ret_text).get("lowest_hkd")
                if out_p is None and out_fields.get("flight_price"):
                    try:
                        out_p = float(
                            re.sub(r"[^\d.]", "", out_fields["flight_price"]) or "0"
                        ) or None
                    except ValueError:
                        out_p = None
                if ret_p is None and ret_fields.get("flight_price"):
                    try:
                        ret_p = float(
                            re.sub(r"[^\d.]", "", ret_fields["flight_price"]) or "0"
                        ) or None
                    except ValueError:
                        ret_p = None
                if out_p is not None and ret_p is not None:
                    recommended_flight_price = round(float(out_p) + float(ret_p), 2)
                else:
                    recommended_flight_price = out_p or ret_p
                flight_card_fields = dict(out_fields)
                # Map return leg into return_* fields for the card
                if ret_fields.get("flight_airline"):
                    flight_card_fields["flight_return_airline"] = ret_fields["flight_airline"]
                if ret_fields.get("flight_airline_logo"):
                    flight_card_fields["flight_return_airline_logo"] = ret_fields[
                        "flight_airline_logo"
                    ]
                if ret_fields.get("flight_depart"):
                    flight_card_fields["flight_return_depart"] = ret_fields["flight_depart"]
                if ret_fields.get("flight_arrive"):
                    flight_card_fields["flight_return_arrive"] = ret_fields["flight_arrive"]
                if ret_fields.get("flight_from"):
                    flight_card_fields["flight_return_from"] = ret_fields["flight_from"]
                else:
                    flight_card_fields["flight_return_from"] = return_from_code.upper()
                if ret_fields.get("flight_to"):
                    flight_card_fields["flight_return_to"] = ret_fields["flight_to"]
                else:
                    flight_card_fields["flight_return_to"] = to_flight_code(origin).upper()
                if ret_fields.get("flight_duration"):
                    flight_card_fields["flight_return_duration"] = ret_fields[
                        "flight_duration"
                    ]
                if ret_fields.get("flight_stops"):
                    flight_card_fields["flight_return_stops"] = ret_fields["flight_stops"]
                flight_card_fields.setdefault("flight_return_date", return_date)
                flight_card_fields.setdefault("flight_date", depart_date)
                # Always pin open-jaw airports (scraper may echo wrong city)
                flight_card_fields["flight_from"] = to_flight_code(origin).upper()
                flight_card_fields["flight_to"] = arrive_code.upper()
                flight_card_fields["flight_return_from"] = return_from_code.upper()
                flight_card_fields["flight_return_to"] = to_flight_code(origin).upper()
                if recommended_flight_price is not None:
                    flight_card_fields["flight_price"] = _fmt_hkd(recommended_flight_price)
                # Persist merged open-jaw card for the GUI (do not lose outbound times)
                self.last_flight_card = {
                    k: v
                    for k, v in flight_card_fields.items()
                    if isinstance(v, str) and k.startswith("flight")
                }
                self.last_flight_card["flight"] = recommended_flight_url or self.last_flight_card.get(
                    "flight", ""
                )
                # Retry once if outbound times never loaded (common after slow pages)
                if not self.last_flight_card.get("flight_depart"):
                    try:
                        saved_return = {
                            k: v
                            for k, v in self.last_flight_card.items()
                            if k.startswith("flight_return") and v
                        }
                        page = self._require_page()
                        page.wait_for_timeout(2500)
                        retry_out = self.search_flights(
                            origin=origin,
                            destination=arrive_code,
                            depart_date=depart_date,
                            trip_type="oneway",
                            adults=adults,
                            include_return_leg=False,
                        )
                        retry_snap = dict(self.last_flight_card or {})
                        retry_fields = extract_booking_urls(retry_out)
                        for k, v in retry_snap.items():
                            if v and not retry_fields.get(k):
                                retry_fields[k] = v
                        for k, v in retry_fields.items():
                            if v and k.startswith("flight") and not k.startswith(
                                "flight_return"
                            ):
                                flight_card_fields[k] = v
                                self.last_flight_card[k] = v
                        # Restore return leg (retry overwrote last_flight_card)
                        for k, v in saved_return.items():
                            self.last_flight_card[k] = v
                            flight_card_fields[k] = v
                        for k, v in (ret_fields or {}).items():
                            if not v:
                                continue
                            mapped = {
                                "flight_airline": "flight_return_airline",
                                "flight_airline_logo": "flight_return_airline_logo",
                                "flight_depart": "flight_return_depart",
                                "flight_arrive": "flight_return_arrive",
                                "flight_duration": "flight_return_duration",
                                "flight_stops": "flight_return_stops",
                            }.get(k)
                            if mapped and not self.last_flight_card.get(mapped):
                                self.last_flight_card[mapped] = v
                                flight_card_fields[mapped] = v
                        self.last_flight_card["flight_return_from"] = return_from_code.upper()
                        self.last_flight_card["flight_return_to"] = to_flight_code(
                            origin
                        ).upper()
                        self.last_flight_card["flight_from"] = to_flight_code(origin).upper()
                        self.last_flight_card["flight_to"] = arrive_code.upper()
                        self.last_flight_card["flight"] = (
                            recommended_flight_url
                            or self.last_flight_card.get("flight", "")
                        )
                    except Exception:
                        pass
                self.last_plan_flight_card = dict(self.last_flight_card)
                flight_prices = summarize_prices("open-jaw", flight_text)
                flight_snippets = flight_prices.get("snippets") or []
                if flight_snippets:
                    recommended_flight_option = flight_snippets[0]
                recommended_flight_airline = flight_card_fields.get("flight_airline", "")
                if recommended_flight_price is not None:
                    transport_lows.append(("flights", float(recommended_flight_price)))
                snip_block = (
                    "\n".join(f"  · {s}" for s in flight_snippets[:4])
                    or "  · (option names sparse on page — use ranked prices below)"
                )
                airline_line = (
                    f"- Outbound airline: {recommended_flight_airline}\n"
                    if recommended_flight_airline
                    else ""
                )
                ret_air = flight_card_fields.get("flight_return_airline", "")
                if ret_air:
                    airline_line += f"- Return airline: {ret_air}\n"
                structured_flight_card = (
                    "Structured flight card:\n"
                    f"- Route: open-jaw {to_flight_code(origin).upper()}→{arrive_code.upper()} "
                    f"/ {return_from_code.upper()}→{to_flight_code(origin).upper()}\n"
                    + (
                        f"- Airline: {recommended_flight_airline}\n"
                        if recommended_flight_airline
                        else ""
                    )
                    + (
                        f"- Airline logo: {flight_card_fields['flight_airline_logo']}\n"
                        if flight_card_fields.get("flight_airline_logo")
                        else ""
                    )
                    + f"- Date: {flight_card_fields.get('flight_date', depart_date)}\n"
                    + f"- From: {flight_card_fields.get('flight_from', to_flight_code(origin).upper())}\n"
                    + f"- To: {flight_card_fields.get('flight_to', arrive_code.upper())}\n"
                    + f"- Depart: {flight_card_fields.get('flight_depart', '')}\n"
                    + f"- Arrive: {flight_card_fields.get('flight_arrive', '')}\n"
                    + f"- Duration: {flight_card_fields.get('flight_duration', '')}\n"
                    + f"- Stops: {flight_card_fields.get('flight_stops', '')}\n"
                    + f"- Return airline: {ret_air}\n"
                    + (
                        f"- Return airline logo: {flight_card_fields['flight_return_airline_logo']}\n"
                        if flight_card_fields.get("flight_return_airline_logo")
                        else ""
                    )
                    + f"- Return date: {flight_card_fields.get('flight_return_date', return_date)}\n"
                    + f"- Return from: {flight_card_fields.get('flight_return_from', return_from_code.upper())}\n"
                    + f"- Return to: {flight_card_fields.get('flight_return_to', to_flight_code(origin).upper())}\n"
                    + f"- Return depart: {flight_card_fields.get('flight_return_depart', '')}\n"
                    + f"- Return arrive: {flight_card_fields.get('flight_return_arrive', '')}\n"
                    + f"- Return duration: {flight_card_fields.get('flight_return_duration', '')}\n"
                    + f"- Return stops: {flight_card_fields.get('flight_return_stops', '')}\n"
                    + f"- Price: {_fmt_hkd(recommended_flight_price)}\n"
                )
                sections.append(
                    f"""{n}) FLIGHTS — OPEN-JAW (live on your dates)
- Region: {regional.label if regional else destination}
- Outbound: {to_flight_code(origin).upper()} → {arrive_code.upper()} on {recommended_flight_date}
- Return: {return_from_code.upper()} → {to_flight_code(origin).upper()} on {return_date}
- Recommended outbound link: {recommended_flight_url}
{airline_line}- Combined lowest seen: {_fmt_hkd(recommended_flight_price)}
- Sample options seen:
{snip_block}
- Note: {flight_compare_text}"""
                )
                booking_lines.append(f"  - Recommended flights: {recommended_flight_url}")
                if structured_flight_card:
                    raw_blocks.insert(0, structured_flight_card)
                raw_blocks.append("--- Raw flight excerpt ---\n" + flight_text[:3500])
                n += 1
            else:
                flight_text = self.search_flights(
                    origin=origin,
                    destination=arrive_code,
                    depart_date=depart_date,
                    return_date=return_date,
                    trip_type="roundtrip",
                    adults=adults,
                )
                flight_url = self._require_page().url
                canon_m = re.search(r"Canonical search URL:\s*(\S+)", flight_text)
                if canon_m:
                    recommended_flight_url = canon_m.group(1).rstrip(".,;")
                else:
                    recommended_flight_url = flight_url or recommended_flight_url
                recommended_flight_date = depart_date
                flight_compare_text = f"Round-trip on requested dates ({depart_date} → {return_date})"
                flight_best_label = flight_compare_text
                flight_prices = summarize_prices(
                    f"{origin}->{arrive_code}", flight_text, url=flight_url
                )
                recommended_flight_price = flight_prices.get("lowest_hkd")
                flight_card_fields = extract_booking_urls(flight_text or "")
                live_rt = dict(self.last_flight_card or {})
                for k, v in live_rt.items():
                    if v and not flight_card_fields.get(k):
                        flight_card_fields[k] = v
                self.last_plan_flight_card = {
                    k: v
                    for k, v in {**live_rt, **flight_card_fields}.items()
                    if isinstance(v, str) and k.startswith("flight")
                }
                if recommended_flight_url:
                    self.last_plan_flight_card["flight"] = recommended_flight_url
                flight_snippets = flight_prices.get("snippets") or []
                if flight_snippets:
                    recommended_flight_option = flight_snippets[0]
                recommended_flight_airline = flight_card_fields.get("flight_airline", "")
                flight_low = recommended_flight_price
                if flight_low is not None:
                    transport_lows.append(("flights", float(flight_low)))
                snip_block = (
                    "\n".join(f"  · {s}" for s in flight_snippets[:4])
                    or "  · (option names sparse on page — use ranked prices below)"
                )
                airline_line = (
                    f"- Airline: {recommended_flight_airline}\n"
                    if recommended_flight_airline
                    else ""
                )
                structured_flight_card = (
                    "Structured flight card:\n"
                    f"- Route: {to_flight_code(origin).upper()}→{arrive_code.upper()} round-trip\n"
                    + (f"- Airline: {recommended_flight_airline}\n" if recommended_flight_airline else "")
                    + f"- From: {flight_card_fields.get('flight_from', to_flight_code(origin).upper())}\n"
                    + f"- To: {flight_card_fields.get('flight_to', arrive_code.upper())}\n"
                    + f"- Depart: {flight_card_fields.get('flight_depart', '')}\n"
                    + f"- Arrive: {flight_card_fields.get('flight_arrive', '')}\n"
                    + f"- Price: {_fmt_hkd(recommended_flight_price)}\n"
                )
                sections.append(
                    f"""{n}) FLIGHTS (live on your dates)
- Route: {to_flight_code(origin).upper()} → {arrive_code.upper()}
- Depart: {recommended_flight_date} | Return: {return_date}
- Recommended link: {recommended_flight_url}
{airline_line}- Lowest seen: {_fmt_hkd(recommended_flight_price)}
- Sample options seen:
{snip_block}
- Note: {flight_compare_text}"""
                )
                booking_lines.append(f"  - Recommended flights: {recommended_flight_url}")
                if structured_flight_card:
                    raw_blocks.insert(0, structured_flight_card)
                raw_blocks.append("--- Raw flight excerpt ---\n" + flight_text[:3500])
                n += 1

        # Attractions after flights so fare scrapes stay reliable
        try:
            if regional:
                merged_cards: list[dict[str, str]] = []
                seen_card: set[str] = set()
                for stay in regional.stays:
                    names = self.scrape_attractions(stay.city, limit=8)[:6]
                    attractions.extend(names)
                    for card in list(self.last_attraction_cards or []):
                        key = (card.get("name") or "").strip().lower()
                        if not key or key in seen_card:
                            continue
                        seen_card.add(key)
                        merged_cards.append(card)
                self.last_attraction_cards = merged_cards
                self.last_attractions = [
                    c.get("name", "") for c in merged_cards if c.get("name")
                ] or attractions
                try:
                    from travel_agent.attraction_images import set_trip_attraction_images

                    set_trip_attraction_images(merged_cards)
                except Exception:
                    pass
            else:
                attractions = self.scrape_attractions(
                    hotel_city or destination, limit=18
                )
        except Exception:
            from travel_agent.destination_guides import filter_attraction_names_for_destination

            stay_city = hotel_city or destination
            attractions = filter_attraction_names_for_destination(
                list(self.last_attractions or []), stay_city
            )
            if attractions != list(self.last_attractions or []):
                self.last_attractions = attractions
                self.last_attraction_cards = [
                    c
                    for c in (self.last_attraction_cards or [])
                    if (c.get("name") or "") in attractions
                ]

        if attractions:
            self._arrange_attractions_route(
                attractions,
                nights,
                city=hotel_city or destination,
                cards=list(self.last_attraction_cards or []),
            )

        train_url = ""
        train_low = None
        if want_trains:
            train_out = self.search_trains(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
            )
            train_url = self._require_page().url
            train_return = self.search_trains(
                origin=destination,
                destination=origin,
                depart_date=return_date,
            )
            train_return_url = self._require_page().url
            train_prices = summarize_prices(
                f"Trains {origin}->{destination}",
                train_out + "\n" + train_return,
                url=train_url,
            )
            train_low = train_prices.get("lowest_hkd")
            # One-way sample x2 as a rough round-trip floor when prices exist.
            train_rt = round(train_low * 2, 2) if train_low is not None else None
            if train_rt is not None:
                transport_lows.append(("trains", float(train_rt)))
            sections.append(
                f"""{n}) TRAINS
- Train search URL (outbound): {train_url}
- Train search URL (return): {train_return_url}
- Open results: [View trains on Trip.com]({train_url})
- Lowest one-way seen: {_fmt_hkd(train_low)}
- Est. round-trip floor (2x lowest one-way): {_fmt_hkd(train_rt)}
- Sample prices: {train_prices.get("prices_hkd", [])[:8]}
- Note: Train availability depends on the corridor; compare with flights."""
            )
            booking_lines.append(f"  - Trains: {train_url}")
            if train_return_url and train_return_url != train_url:
                booking_lines.append(f"  - Return trains: {train_return_url}")
            raw_blocks.append(
                "--- Raw train excerpt ---\n"
                + train_out[:1600]
                + "\n\n"
                + train_return[:1600]
            )
            n += 1

        transfer_url = ""
        transfer_low = None
        if want_transfers:
            transfer_text = self.search_transfers(
                location=hotel_city,
                date=depart_date,
            )
            transfer_url = self._require_page().url
            transfer_prices = summarize_prices(
                f"Airport transfers in {hotel_city}",
                transfer_text,
                url=transfer_url,
            )
            transfer_low = transfer_prices.get("lowest_hkd")
            sections.append(
                f"""{n}) AIRPORT TRANSFERS / LOCAL GROUND
- Airport transfer search URL: {transfer_url}
- Open results: [View airport transfers on Trip.com]({transfer_url})
- Lowest seen: {_fmt_hkd(transfer_low)}
- Sample prices: {transfer_prices.get("prices_hkd", [])[:8]}
- Use for airport↔hotel rides when not renting a car."""
            )
            booking_lines.append(f"  - Airport transfers: {transfer_url}")
            raw_blocks.append("--- Raw transfer excerpt ---\n" + transfer_text[:1800])
            n += 1

        # One hotel search on the trip dates (skip multi-date compares — each
        # hub-form search is ~20–40s and made the GUI look hung).
        stay0_nights = (
            regional.stays[0].nights if regional and regional.stays else nights
        )
        try:
            stay0_checkout = (
                date.fromisoformat(depart_date) + timedelta(days=stay0_nights)
            ).isoformat()
        except ValueError:
            stay0_checkout = return_date
        recommended_hotel_checkin = depart_date
        recommended_hotel_price = None
        recommended_hotel_url = ""
        recommended_hotel_option = ""
        recommended_hotel_detail_links: list[str] = []
        hotel_best_label = f"{hotel_city} | {depart_date} -> {stay0_checkout}"
        hotel_compare_text = (
            f"Hotel search on requested dates only ({depart_date} → {stay0_checkout})"
        )
        rec_checkout = stay0_checkout

        hotel_text = self.search_hotels(
            city=(regional.stays[0].city if regional and regional.stays else hotel_city),
            checkin=recommended_hotel_checkin,
            checkout=rec_checkout,
            adults=adults,
            rooms=1,
        )
        hotel_url = self._require_page().url
        # Prefer hotel detail page from Playwright; fall back to live list
        recommended_hotel_url = (
            getattr(self, "last_hotel_detail_url", "") or ""
        ).strip()
        if not recommended_hotel_url:
            recommended_hotel_url = (
                getattr(self, "last_hotel_list_url", "") or ""
            ).strip()
        if not recommended_hotel_url:
            canon_h = re.search(r"Canonical search URL:\s*(\S+)", hotel_text)
            if canon_h:
                recommended_hotel_url = canon_h.group(1).rstrip(".,;")
            else:
                recommended_hotel_url = hotel_url or recommended_hotel_url
        recommended_hotel_url = ensure_locale_curr(
            normalize_trip_url(recommended_hotel_url)
        )
        # Seed detail link from search_hotels before scanning the page again
        if getattr(self, "last_hotel_detail_url", ""):
            recommended_hotel_detail_links = [self.last_hotel_detail_url]
            if not recommended_hotel_url or "/hotels/list" in recommended_hotel_url.lower():
                recommended_hotel_url = self.last_hotel_detail_url
        hotel_prices = summarize_prices(
            f"Hotels in {hotel_city}",
            hotel_text,
            url=hotel_url,
        )
        if recommended_hotel_price is None:
            recommended_hotel_price = hotel_prices.get("lowest_hkd")
        hotel_low = recommended_hotel_price
        hotel_total = (
            round(hotel_low * stay0_nights, 2) if hotel_low is not None else None
        )
        hotel_snippets = hotel_prices.get("snippets") or []
        if hotel_snippets:
            recommended_hotel_option = hotel_snippets[0]
        seeded_detail = list(recommended_hotel_detail_links)
        recommended_hotel_detail_links = []
        recommended_hotel_names: list[str] = []
        hotel_option_pairs: list[tuple[str, str]] = []
        for u, label in self._extract_hotel_detail_options(limit=8):
            nu = ensure_locale_curr(normalize_trip_url(u))
            low = nu.lower()
            if "/hotels/" not in low:
                continue
            if not (
                "hotelid=" in low
                or re.search(r"hotel-detail-\d+", low, re.I)
                or re.search(r"/hotels/[^/?]+-\d+", low)
            ):
                continue
            hotel_option_pairs.append((nu, label))
            recommended_hotel_detail_links.append(nu)
            if (
                label
                and label not in recommended_hotel_names
                and _hotel_name_plausible_for_city(label, hotel_city)
            ):
                recommended_hotel_names.append(label)
        if len(hotel_option_pairs) > 1:
            picked_url, picked_name = self._pick_hotel_from_options(
                hotel_option_pairs,
                page_text=hotel_text,
                city=hotel_city,
                prices=hotel_prices.get("prices_hkd") or [],
            )
            if picked_url:
                recommended_hotel_detail_links = [picked_url] + [
                    u for u, _ in hotel_option_pairs if u != picked_url
                ]
                if picked_name:
                    recommended_hotel_names = [picked_name] + [
                        n for n in recommended_hotel_names if n != picked_name
                    ]
        if not recommended_hotel_detail_links and seeded_detail:
            recommended_hotel_detail_links = seeded_detail
        # Open the LLM-selected hotel DETAIL page (same as search_hotels)
        if recommended_hotel_detail_links:
            from travel_agent.trip_urls import (
                canonicalize_hotel_detail_url,
                is_trusted_hotel_detail_url,
            )

            detail0 = (
                canonicalize_hotel_detail_url(
                    recommended_hotel_detail_links[0],
                    checkin=recommended_hotel_checkin,
                    checkout=rec_checkout,
                    city=hotel_city,
                )
                or recommended_hotel_detail_links[0]
            )
            recommended_hotel_detail_links[0] = detail0
            if is_trusted_hotel_detail_url(detail0):
                meta = self._scrape_hotel_detail_meta(detail0)
                if meta.get("name") and _hotel_name_plausible_for_city(
                    meta["name"], hotel_city
                ):
                    recommended_hotel_names = [meta["name"]] + [
                        n for n in recommended_hotel_names if n != meta["name"]
                    ]
                    self.last_hotel_name = meta["name"]
                self.last_hotel_detail_url = detail0
                stay0_meta = meta
            else:
                stay0_meta = {}
        else:
            stay0_meta = {}
        # Prefer real hotel title over generic price snippets
        if recommended_hotel_names:
            recommended_hotel_option = recommended_hotel_names[0]

        # Prefer picked-hotel meta (HTTP/list-card by hotelId) over first list card
        stay0_card = {
            "name": (stay0_meta or {}).get("name")
            or (
                recommended_hotel_names[0] if recommended_hotel_names else ""
            ),
            "score": (stay0_meta or {}).get("score", ""),
            "score_label": (stay0_meta or {}).get("score_label", ""),
            "stars": (stay0_meta or {}).get("stars", ""),
            "reviews": (stay0_meta or {}).get("reviews", ""),
            "image_url": (stay0_meta or {}).get("image_url", ""),
            "price_label": (stay0_meta or {}).get("price_label", ""),
            "location": (stay0_meta or {}).get("location", hotel_city),
        }
        if not stay0_card.get("name") or not stay0_card.get("image_url"):
            top = self._scrape_top_hotel_card(
                city=hotel_city,
                fallback_name=stay0_card.get("name") or f"Hotels in {hotel_city}",
                lowest=float(hotel_low) if hotel_low is not None else None,
            )
            for k, v in top.items():
                if v and not stay0_card.get(k):
                    stay0_card[k] = v
        if stay0_card.get("name") and _hotel_name_plausible_for_city(
            stay0_card["name"], hotel_city
        ):
            if not recommended_hotel_names:
                recommended_hotel_names = [stay0_card["name"]]
                recommended_hotel_option = stay0_card["name"]
        stay0_image = stay0_card.get("image_url") or ""
        if recommended_hotel_detail_links:
            try:
                picked_img = self.scrape_hotel_image_url(
                    recommended_hotel_detail_links[0]
                )
            except Exception:
                picked_img = ""
            if picked_img:
                stay0_image = picked_img
        if not stay0_image:
            stay0_image = _city_hotel_fallback_image(
                hotel_city,
                hotel_name=(
                    recommended_hotel_names[0] if recommended_hotel_names else ""
                ),
            )

        snip_hotels = (
            "\n".join(f"  · {s}" for s in hotel_snippets[:5])
            or "  · (hotel names sparse on page — use ranked nightly rates below)"
        )
        detail_block = (
            "\n".join(f"  · {u}" for u in recommended_hotel_detail_links)
            or f"  · {recommended_hotel_url}"
        )
        name_line = (
            f"- Recommended hotel name: {recommended_hotel_names[0]}\n"
            if recommended_hotel_names
            else ""
        )
        sections.append(
            f"""{n}) HOTELS (compared live)
- Recommended check-in: {recommended_hotel_checkin} ({stay0_nights} nights)
- Recommended hotel list link: {recommended_hotel_url}
{name_line}- Hotel detail links found:
{detail_block}
- Lowest nightly seen: {_fmt_hkd(hotel_low)}
- Est. stay total (lowest x nights): {_fmt_hkd(hotel_total)}
- Best row: {hotel_best_label or "see ranking below"}
- Sample hotel options / rates seen:
{snip_hotels}
- Ranking:
{hotel_compare_text.split("Notes:")[0].strip()}"""
        )
        booking_lines.append(f"  - Recommended hotels: {recommended_hotel_url}")
        if recommended_hotel_detail_links:
            booking_lines.append(
                f"  - Recommended hotel detail link: {recommended_hotel_detail_links[0]}"
            )
        for detail in recommended_hotel_detail_links[:2]:
            booking_lines.append(f"  - Hotel option: {detail}")
        raw_blocks.append("--- Raw hotel excerpt ---\n" + hotel_text[:2200])
        n += 1

        # Seed first stay; for regional trips, search hotels in each later city too
        self.last_hotel_stays = [
            {
                "city": hotel_city,
                "airport": arrive_code,
                "nights": str(
                    regional.stays[0].nights if regional and regional.stays else nights
                ),
                "checkin": recommended_hotel_checkin,
                "checkout": (
                    (
                        date.fromisoformat(recommended_hotel_checkin)
                        + timedelta(
                            days=(
                                regional.stays[0].nights
                                if regional and regional.stays
                                else nights
                            )
                        )
                    ).isoformat()
                    if recommended_hotel_checkin
                    else return_date
                ),
                "label": (
                    regional.stays[0].label
                    if regional and regional.stays
                    else f"Stay · {hotel_city}"
                ),
                "name": (
                    (getattr(self, "last_hotel_name", "") or "").strip()
                    or (
                        stay0_card.get("name")
                        if stay0_card.get("name")
                        and _hotel_name_plausible_for_city(stay0_card["name"], hotel_city)
                        and not stay0_card["name"].lower().startswith(
                            ("hotels in ", "recommended hotel")
                        )
                        else ""
                    )
                    or (
                        recommended_hotel_names[0]
                        if recommended_hotel_names
                        else ""
                    )
                    or f"Hotels in {hotel_city}"
                ),
                "url": (
                    (getattr(self, "last_hotel_detail_url", "") or "").strip()
                    or (
                        recommended_hotel_detail_links[0]
                        if recommended_hotel_detail_links
                        else ""
                    )
                ),
                "price_label": (
                    stay0_card.get("price_label")
                    or (f"HK${hotel_low:,.0f}" if hotel_low is not None else "")
                ),
                "total_label": (
                    f"Est. stay total: HK${hotel_total:,.0f}"
                    if hotel_total is not None
                    else ""
                ),
                "location": stay0_card.get("location") or hotel_city,
                "image_url": stay0_image,
                "score": stay0_card.get("score", ""),
                "score_label": stay0_card.get("score_label", ""),
                "stars": stay0_card.get("stars", ""),
                "reviews": stay0_card.get("reviews", ""),
            }
        ]
        # Prefer detail URL for stay0; only fall back to list if no hotelId link
        from travel_agent.trip_urls import is_trusted_hotel_detail_url as _is_detail

        if not _is_detail(self.last_hotel_stays[0].get("url") or ""):
            list_fallback = (
                (getattr(self, "last_hotel_list_url", "") or "").strip()
                or recommended_hotel_url
            )
            if list_fallback:
                self.last_hotel_stays[0]["url"] = list_fallback
        if regional and len(regional.stays) > 1:
            extra_blocks: list[str] = []
            for si, stay in enumerate(regional.stays[1:], start=2):
                cin = stay.checkin or depart_date
                cout = stay.checkout or return_date
                stay_rec = self._build_regional_stay_hotel(
                    stay_city=stay.city,
                    airport=stay.airport,
                    nights=stay.nights,
                    checkin=cin,
                    checkout=cout,
                    label=stay.label or f"Stay {si} · {stay.city}",
                    adults=adults,
                    stay_index=si,
                )
                raw_excerpt = stay_rec.pop("_raw_excerpt", "")
                self.last_hotel_stays.append(stay_rec)
                booking_lines.append(
                    f"  - Stay {si} hotel ({stay.city}): "
                    f"{stay_rec.get('url') or '(open Trip.com hotels)'}"
                )
                extra_blocks.append(
                    f"Stay {si} · {stay.city} ({stay.nights} nights)\n"
                    f"- Check-in: {cin} → {cout}\n"
                    f"- Airport hub: {stay.airport.upper()}\n"
                    f"- Hotel: {stay_rec['name']}\n"
                    f"- Lowest nightly: {stay_rec.get('price_label') or 'see Trip.com'}\n"
                    f"- Est. stay total: {stay_rec.get('total_label') or 'see Trip.com'}\n"
                    f"- Book: {stay_rec.get('url') or 'open Trip.com hotels list'}"
                )
                if raw_excerpt:
                    raw_blocks.append(raw_excerpt)
            # Fix first stay nights to regional split (was full trip length)
            if regional.stays:
                first = regional.stays[0]
                self.last_hotel_stays[0]["nights"] = str(first.nights)
                self.last_hotel_stays[0]["checkin"] = first.checkin or recommended_hotel_checkin
                self.last_hotel_stays[0]["checkout"] = first.checkout or ""
                self.last_hotel_stays[0]["label"] = first.label
                self.last_hotel_stays[0]["airport"] = first.airport
            if extra_blocks:
                sections.append(
                    f"""{n}) ADDITIONAL CITY HOTELS
- Region: {regional.label}
- Internal travel: {regional.internal_note}
{chr(10).join(extra_blocks)}"""
                )
                n += 1
            # Restore stay-0 as the global hotel pointer so booking_links /
            # single-card consumers are not left on the last city's list URL.
            first_stay = self.last_hotel_stays[0] if self.last_hotel_stays else {}
            first_url = (first_stay.get("url") or "").strip()
            from travel_agent.trip_urls import is_trusted_hotel_detail_url as _ok_detail

            if first_url and _ok_detail(first_url):
                self.last_hotel_detail_url = first_url
            if first_stay.get("name"):
                self.last_hotel_name = first_stay["name"]
            # Keep last list URL from stay 0 if we still have it on the record
            if first_url and "/hotels/list" in first_url.lower():
                self.last_hotel_list_url = first_url

        car_low = None
        car_total = None
        if need_car:
            car_pickup_city = (
                regional.stays[0].city
                if regional and regional.stays
                else hotel_city
            )
            car_text = self.search_cars(
                location=car_pickup_city,
                pickup_date=depart_date,
                dropoff_date=return_date,
            )
            car_url = (
                getattr(self, "last_car_detail_url", "") or ""
            ).strip() or self._require_page().url
            car_card = dict(getattr(self, "last_car_card", None) or {})
            car_prices = summarize_prices(
                f"Car rental in {car_pickup_city}",
                car_text,
                url=car_url,
            )
            car_low = car_prices.get("lowest_hkd")
            if not car_low and car_card.get("price_label"):
                parsed_daily = parse_prices(car_card["price_label"])
                if parsed_daily:
                    car_low = parsed_daily[0]
            car_total = (
                round(car_low * nights, 2) if car_low is not None else None
            )
            if car_card.get("total_label"):
                tot_p = parse_prices(car_card["total_label"])
                if tot_p:
                    car_total = tot_p[0]
            name_line = (
                f"- Recommended car: {car_card.get('name')}\n"
                if car_card.get("name")
                else ""
            )
            detail_line = (
                f"- Car detail link: {car_url}\n"
                if self._car_detail_url_ok(car_url)
                else f"- Car search URL: {car_url}\n"
            )
            sections.append(
                f"""{n}) CAR RENTAL
{name_line}{detail_line}- Open deal: [View car rental on Trip.com]({car_url})
- Lowest daily seen: {_fmt_hkd(car_low)}
- Est. rental total: {_fmt_hkd(car_total)}
- Sample prices: {car_prices.get("prices_hkd", [])[:8]}"""
            )
            booking_lines.append(f"  - Cars: {car_url}")
            if car_card.get("name"):
                booking_lines.append(f"  - Recommended car name: {car_card['name']}")
            if self._car_detail_url_ok(car_url):
                booking_lines.append(f"  - Recommended car detail link: {car_url}")
            raw_blocks.append("--- Raw car rental excerpt ---\n" + car_text[:2200])
            n += 1

        best_transport_name = None
        best_transport_cost = None
        if transport_lows:
            best_transport_name, best_transport_cost = min(
                transport_lows, key=lambda item: item[1]
            )

        trip_total = None
        if best_transport_cost is not None and hotel_total is not None:
            trip_total = round(best_transport_cost + hotel_total, 2)
            if car_total is not None:
                trip_total = round(trip_total + car_total, 2)
            if transfer_low is not None and not need_car:
                # Rough round-trip airport transfers when not driving.
                trip_total = round(trip_total + (transfer_low * 2), 2)

        transport_compare = ", ".join(
            f"{name} {_fmt_hkd(cost)}" for name, cost in transport_lows
        ) or "n/a"
        budget_parts = []
        if best_transport_name:
            budget_parts.append(f"best transport ({best_transport_name})")
        budget_parts.append("hotel")
        if need_car:
            budget_parts.append("car")
        elif want_transfers and transfer_low is not None:
            budget_parts.append("transfers")
        budget_label = " + ".join(budget_parts)

        budget_note = "No budget set."
        if budget_hkd is not None and trip_total is not None:
            if trip_total <= budget_hkd:
                budget_note = (
                    f"Estimated low end {_fmt_hkd(trip_total)} fits budget "
                    f"{_fmt_hkd(budget_hkd)}."
                )
            else:
                over = trip_total - budget_hkd
                budget_note = (
                    f"Estimated low end {_fmt_hkd(trip_total)} is about "
                    f"{_fmt_hkd(over)} over budget {_fmt_hkd(budget_hkd)}. "
                    "Suggest flexible dates, trains vs flights, or nearby cities."
                )

        interests_line = interests.strip() or "general sightseeing, food, local transport"
        modes = []
        if want_flights:
            modes.append("flights")
        if want_trains:
            modes.append("trains")
        if want_transfers:
            modes.append("airport transfers")
        if need_car:
            modes.append("rental car")

        sections.append(
            f"""{n}) TRANSPORT COMPARISON + BUDGET
- Modes considered: {", ".join(modes)}
- Transport options seen: {transport_compare}
- Cheapest long-haul option: {best_transport_name or "n/a"} ({_fmt_hkd(best_transport_cost)})
- Est. low-end trip ({budget_label}): {_fmt_hkd(trip_total)}
- {budget_note}"""
        )
        n += 1
        day_lines = _build_suggested_day_flow(
            nights,
            interests=interests or "",
            destination=hotel_city or destination,
            attractions=attractions or None,
            attraction_plan=getattr(self, "last_attraction_day_plan", None) or None,
            regional_route=regional,
            arrive_time=flight_card_fields.get("flight_arrive", "") or "",
            return_depart_time=flight_card_fields.get("flight_return_depart", "") or "",
        )
        sections.append(
            f"""{n}) SUGGESTED DAY FLOW
{day_lines}"""
        )
        if getattr(self, "last_attraction_day_plan", None):
            sections.append(
                "LLM-arranged attraction route (from Trip.com search):\n"
                + "\n".join(
                    f"- Day {i}: {d.get('go', '')}"
                    + (f" + {d['also']}" if d.get("also") and d.get("also") != d.get("go") else "")
                    + (f" ({d['route']})" if d.get("route") else "")
                    for i, d in enumerate(self.last_attraction_day_plan, 1)
                )
            )
        if attractions:
            lines = ["Attraction sources (Trip.com Attractions tab + detail pages):"]
            cards = list(getattr(self, "last_attraction_cards", None) or [])
            if cards:
                for a in cards[:12]:
                    bits = [f"- {a.get('name', '')}"]
                    if a.get("visit_time"):
                        bits.append(f"visit {a['visit_time']}")
                    if a.get("open_hours"):
                        bits.append(f"open {a['open_hours']}")
                    if a.get("address"):
                        bits.append(a["address"])
                    lines.append(" · ".join(bits))
            else:
                lines.extend(f"- {a}" for a in attractions[:12])
            sections.append("\n".join(lines))
        n += 1

        pick_lines = [
            "======= YOUR RECOMMENDED BOOKINGS =======",
        ]
        if want_flights:
            pick_lines.append("RECOMMENDED FLIGHT")
            if arrive_code.upper() != return_from_code.upper():
                pick_lines.append(
                    f"- Route: open-jaw {to_flight_code(origin).upper()}→{arrive_code.upper()} "
                    f"/ {return_from_code.upper()}→{to_flight_code(origin).upper()}"
                )
            else:
                pick_lines.append(
                    f"- Route: {to_flight_code(origin).upper()} -> {arrive_code.upper()} (round-trip)"
                )
            if recommended_flight_airline:
                pick_lines.append(f"- Airline: {recommended_flight_airline}")
            if flight_card_fields.get("flight_airline_logo"):
                pick_lines.append(
                    f"- Airline logo: {flight_card_fields['flight_airline_logo']}"
                )
            pick_lines.append(
                f"- Date: {flight_card_fields.get('flight_date') or recommended_flight_date}"
            )
            if flight_card_fields.get("flight_depart"):
                pick_lines.append(f"- Depart: {flight_card_fields['flight_depart']}")
            if flight_card_fields.get("flight_arrive"):
                pick_lines.append(f"- Arrive: {flight_card_fields['flight_arrive']}")
            if flight_card_fields.get("flight_from"):
                pick_lines.append(f"- From: {flight_card_fields['flight_from']}")
            if flight_card_fields.get("flight_to"):
                pick_lines.append(f"- To: {flight_card_fields['flight_to']}")
            if flight_card_fields.get("flight_duration"):
                pick_lines.append(f"- Duration: {flight_card_fields['flight_duration']}")
            if flight_card_fields.get("flight_stops"):
                pick_lines.append(f"- Stops: {flight_card_fields['flight_stops']}")
            if flight_card_fields.get("flight_return_airline"):
                pick_lines.append(
                    f"- Return airline: {flight_card_fields['flight_return_airline']}"
                )
            if flight_card_fields.get("flight_return_airline_logo"):
                pick_lines.append(
                    f"- Return airline logo: {flight_card_fields['flight_return_airline_logo']}"
                )
            pick_lines.append(
                f"- Return date: {flight_card_fields.get('flight_return_date') or return_date}"
            )
            if flight_card_fields.get("flight_return_depart"):
                pick_lines.append(
                    f"- Return depart: {flight_card_fields['flight_return_depart']}"
                )
            if flight_card_fields.get("flight_return_arrive"):
                pick_lines.append(
                    f"- Return arrive: {flight_card_fields['flight_return_arrive']}"
                )
            if flight_card_fields.get("flight_return_from"):
                pick_lines.append(
                    f"- Return from: {flight_card_fields['flight_return_from']}"
                )
            if flight_card_fields.get("flight_return_to"):
                pick_lines.append(
                    f"- Return to: {flight_card_fields['flight_return_to']}"
                )
            if flight_card_fields.get("flight_return_duration"):
                pick_lines.append(
                    f"- Return duration: {flight_card_fields['flight_return_duration']}"
                )
            if flight_card_fields.get("flight_return_stops"):
                pick_lines.append(
                    f"- Return stops: {flight_card_fields['flight_return_stops']}"
                )
            pick_lines.append(f"- Lowest seen: {_fmt_hkd(recommended_flight_price)}")
            if recommended_flight_option:
                pick_lines.append(f"- Option seen: {recommended_flight_option}")
            pick_lines.append(f"- Book this flight search: {recommended_flight_url}")
            if structured_flight_card:
                pick_lines.append("")
                pick_lines.append(structured_flight_card)
            pick_lines.append("")
        pick_lines.append("RECOMMENDED HOTEL")
        if self.last_hotel_stays and len(self.last_hotel_stays) > 1:
            pick_lines.append(
                f"- Multi-city stays: {len(self.last_hotel_stays)} hotels"
            )
            for i, stay in enumerate(self.last_hotel_stays, 1):
                pick_lines.append("")
                pick_lines.append(f"Stay {i} · {stay.get('city', '')}")
                pick_lines.append(f"- Hotel: {stay.get('name', '')}")
                pick_lines.append(
                    f"- Check-in: {stay.get('checkin', '')} → {stay.get('checkout', '')} "
                    f"({stay.get('nights', '')} nights)"
                )
                pick_lines.append(f"- Location: {stay.get('location', stay.get('city', ''))}")
                if stay.get("price_label"):
                    pick_lines.append(f"- Lowest nightly: {stay['price_label']}")
                if stay.get("total_label"):
                    pick_lines.append(f"- {stay['total_label']}")
                pick_lines.append(f"- Book this hotel: {stay.get('url', '')}")
        else:
            pick_lines.append(f"- City: {hotel_city}")
            pick_lines.append(
                f"- Check-in: {recommended_hotel_checkin} ({stay0_nights} nights)"
            )
            pick_lines.append(f"- Lowest nightly: {_fmt_hkd(hotel_low)}")
            pick_lines.append(f"- Est. stay total: {_fmt_hkd(hotel_total)}")
            if recommended_hotel_option:
                pick_lines.append(f"- Option seen: {recommended_hotel_option}")
            pick_lines.append(f"- Book this hotel search: {recommended_hotel_url}")
            for i, detail in enumerate(recommended_hotel_detail_links[:3], 1):
                pick_lines.append(f"- Hotel option link {i}: {detail}")
        pick_lines.append("")
        if want_trains and train_url:
            pick_lines.append(
                f"ALTERNATIVE — Trains (lowest one-way {_fmt_hkd(train_low)}): {train_url}"
            )
            pick_lines.append("")
        if need_car:
            pick_lines.append("Car rental was included — see Cars URL below.")
            pick_lines.append("")
        pick_lines.append("Other Trip.com links:")
        pick_lines.extend(booking_lines)

        recommend_block = "\n".join(pick_lines)
        sections.append(
            f"""{n}) RECOMMENDED FLIGHT + HOTEL (with links)
{recommend_block}"""
        )

        header = f"""TRIP PLAN (Trip.com Hong Kong live search)
========================================
Route: {origin} -> {destination}
Requested dates: {depart_date} -> {return_date} ({nights} nights)
Travelers: {adults} adult(s)
Hotel city: {hotel_city}
Interests: {interests_line}
Transport modes: {", ".join(modes)}

{route_preamble}

{recommend_block}
"""
        if regional and arrive_code.upper() != return_from_code.upper():
            header = f"""TRIP PLAN (Trip.com Hong Kong live search)
========================================
Region: {regional.label} (multi-city)
Flights: {to_flight_code(origin).upper()} → {arrive_code.upper()} in / {return_from_code.upper()} → {to_flight_code(origin).upper()} out
Stays: {", ".join(s.label for s in regional.stays)}
Internal: {regional.internal_note}
Requested dates: {depart_date} -> {return_date} ({nights} nights)
Travelers: {adults} adult(s)
Interests: {interests_line}
Transport modes: {", ".join(modes)}

{route_preamble}

{recommend_block}
"""
        plan = header + "\n\n" + "\n\n".join(sections)
        return _clean_text(
            plan + "\n\n" + "\n\n".join(raw_blocks),
            limit=16000,
        )

    def click_text(self, text: str) -> str:
        page = self._require_page()
        locator = page.get_by_text(text, exact=False).first
        locator.click(timeout=8000)
        page.wait_for_timeout(2000)
        return f"Clicked text matching '{text}'. Now at: {page.url}\n\n{self.get_page_summary()}"

    def _dismiss_popups(self) -> None:
        page = self._require_page()
        candidates = [
            "button:has-text('Accept')",
            "button:has-text('Agree')",
            "button:has-text('Got it')",
            "button:has-text('OK')",
            "button:has-text('Close')",
            "[aria-label='Close']",
            ".close",
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=1000)
                    page.wait_for_timeout(300)
            except Exception:
                continue

    def _wait_for_results(self, keywords: list[str], attempts: int = 8) -> None:
        """Poll the page until price-like / result text appears."""
        page = self._require_page()
        for _ in range(attempts):
            try:
                text = page.inner_text("body").lower()
            except Exception:
                text = ""
            if any(k.lower() in text for k in keywords) and len(text) > 400:
                return
            page.wait_for_timeout(1500)

    def _try_fill_hotel_form(
        self,
        city: str,
        checkin: str,
        checkout: str,
        adults: int,
        rooms: int,
    ) -> bool:
        """Type destination into Trip.com hotels hub (Where to?) and Search."""
        page = self._require_page()
        city = typed_place_name(city)
        if not city:
            return False
        try:
            # Match the hub "Where to?" field (and older City/Destination labels)
            city_box = page.locator(
                "input[placeholder*='Where to' i], "
                "input[placeholder*='City' i], "
                "input[placeholder*='Destination' i], "
                "input[placeholder*='Hotel' i], "
                "input[aria-label*='Where to' i], "
                "input[aria-label*='City' i], "
                "input[aria-label*='Destination' i], "
                "input[aria-label*='hotel destination' i]"
            ).first
            if not city_box.count():
                # Hub search bar: first visible text input in the main form
                city_box = page.locator(
                    "form input[type='text'], "
                    "[class*='search'] input[type='text'], "
                    "input[type='text']"
                ).first
            if not city_box.count():
                return False

            city_box.click(timeout=8000)
            page.wait_for_timeout(300)
            try:
                city_box.fill("")
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            # Type so autocomplete suggestions appear (spaces, never '+')
            try:
                city_box.type(city, delay=55)
            except Exception:
                city_box.fill(city)
            page.wait_for_timeout(1600)

            # Prefer a suggestion that contains the city name
            picked = False
            city_l = city.lower()
            try:
                suggestions = page.locator(
                    "[class*='suggest'] li, "
                    "[class*='Suggest'] li, "
                    "[class*='autocomplete'] li, "
                    "[class*='AutoComplete'] li, "
                    "[class*='dropdown'] li, "
                    "[role='listbox'] [role='option'], "
                    "[role='option'], "
                    "[class*='destination'] li, "
                    "ul[class*='list'] li"
                )
                n = min(suggestions.count(), 14)
                for i in range(n):
                    try:
                        el = suggestions.nth(i)
                        if not el.is_visible(timeout=400):
                            continue
                        text = (el.inner_text(timeout=500) or "").strip()
                        if not text:
                            continue
                        low = text.lower()
                        # Prefer city / destination rows over hotel-name hits
                        if city_l in low or city_l.split()[0] in low:
                            el.click(timeout=3000)
                            picked = True
                            break
                    except Exception:
                        continue
                if not picked and n > 0:
                    # First visible suggestion (Trip.com ranks best match first)
                    for i in range(n):
                        try:
                            el = suggestions.nth(i)
                            if el.is_visible(timeout=300):
                                el.click(timeout=3000)
                                picked = True
                                break
                        except Exception:
                            continue
            except Exception:
                picked = False

            if not picked:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(250)
                page.keyboard.press("Enter")
            page.wait_for_timeout(700)

            # Best-effort date + occupancy (hub often already has defaults)
            self._try_set_hotel_hub_dates(checkin, checkout)
            self._try_set_hotel_hub_occupancy(adults, rooms)

            search_btn = page.get_by_role("button", name=re.compile(r"search", re.I))
            if search_btn.count():
                search_btn.first.click(timeout=8000)
            else:
                btn = page.locator(
                    "button:has-text('Search'), "
                    "[class*='search'] button, "
                    "button[type='submit']"
                ).first
                if btn.count():
                    btn.click(timeout=8000)
                else:
                    city_box.press("Enter")

            # Wait until we leave the hub or results hydrate
            try:
                page.wait_for_url(re.compile(r"/hotels/(list|detail)", re.I), timeout=20000)
            except Exception:
                page.wait_for_timeout(5000)
            # Results often hydrate after the URL change
            for _ in range(8):
                try:
                    probe = page.inner_text("body")
                except Exception:
                    probe = ""
                if any(p >= 200 for p in parse_prices(probe)):
                    break
                if re.search(r"\d+\s*properties|\d+\s*hotels|HK\$\s*\d", probe, re.I):
                    page.wait_for_timeout(800)
                    break
                page.wait_for_timeout(700)
            page.wait_for_timeout(800)
            return self._hotel_list_url_ok(page.url or "")
        except Exception:
            return False

    def _try_set_hotel_hub_dates(self, checkin: str, checkout: str) -> None:
        """Best-effort date selection on the hotels hub (calendar UI varies)."""
        page = self._require_page()
        try:
            # Some builds expose hidden/date inputs
            for sel, val in (
                ("input[name*='checkin' i], input[placeholder*='Check-in' i]", checkin),
                ("input[name*='checkout' i], input[placeholder*='Check-out' i]", checkout),
            ):
                box = page.locator(sel).first
                if box.count():
                    try:
                        box.fill(val, timeout=1500)
                    except Exception:
                        pass
        except Exception:
            pass

    def _try_set_hotel_hub_occupancy(self, adults: int, rooms: int) -> None:
        """Best-effort rooms/adults on the hotels hub."""
        # Hub defaults (1 room, 2 adults) match our usual search; skip fragile UI.
        _ = (adults, rooms)
        return

    def _try_fill_train_form(self, origin: str, destination: str, depart_date: str) -> None:
        page = self._require_page()
        inputs = page.locator("input")
        count = min(inputs.count(), 8)
        if count >= 2:
            inputs.nth(0).click()
            inputs.nth(0).fill(origin)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
            inputs.nth(1).click()
            inputs.nth(1).fill(destination)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
        search_btn = page.get_by_role("button", name=re.compile("search", re.I))
        if search_btn.count():
            search_btn.first.click()
            page.wait_for_timeout(3000)

    def _click_outbound_select(self, outbound: dict[str, str]) -> bool:
        """Click the Select control for a specific outbound flight row."""
        page = self._require_page()
        dep = (outbound.get("depart_time") or "").strip()
        airline = (outbound.get("airline") or "").strip()
        price = (outbound.get("price_label") or "").replace(" ", "")
        price_digits = re.sub(r"[^\d]", "", price)

        selects = page.get_by_text(re.compile(r"^Select$", re.I))
        count = min(selects.count(), 40)
        best_i = -1
        best_score = -1
        for i in range(count):
            el = selects.nth(i)
            try:
                if not el.is_visible(timeout=400):
                    continue
                blob = el.evaluate(
                    """(node) => {
                        let n = node;
                        for (let k = 0; k < 8 && n; k++) {
                          n = n.parentElement;
                          if (!n) break;
                          const t = (n.innerText || '');
                          if (t.length > 40 && t.length < 1200) return t;
                        }
                        return (node.parentElement && node.parentElement.innerText) || '';
                    }"""
                )
            except Exception:
                continue
            text = (blob or "").replace("\xa0", " ")
            score = 0
            if dep and dep in text:
                score += 3
            if airline and airline.lower() in text.lower():
                score += 2
            if price_digits and price_digits in re.sub(r"[^\d]", "", text):
                score += 2
            if score > best_score:
                best_score = score
                best_i = i
        if best_i < 0 or best_score < 3:
            # Fallback: first visible Select in the results list
            for i in range(count):
                try:
                    el = selects.nth(i)
                    if el.is_visible(timeout=300):
                        el.click(timeout=4000)
                        return True
                except Exception:
                    continue
            return False
        try:
            selects.nth(best_i).click(timeout=4000)
            return True
        except Exception:
            return False

    def _scrape_return_flight_card(
        self,
        *,
        outbound: dict[str, str],
        origin: str = "",
        destination: str = "",
    ) -> dict[str, str]:
        """After outbound list, open return options and scrape the cheapest inbound."""
        page = self._require_page()
        if not self._click_outbound_select(outbound):
            return {}

        # Wait until inbound results finish loading (not just the "Returning to" shell)
        body = ""
        rows: list[dict[str, str]] = []
        for _ in range(40):
            page.wait_for_timeout(1000)
            self._dismiss_popups()
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            loading = bool(
                re.search(
                    r"(?i)loading the best deals|finding flexible ticket|searching for this route",
                    body,
                )
            )
            rows = parse_trip_com_flight_rows(
                body[:25000], origin=origin, destination=destination
            )
            has_list = bool(
                rows
                or re.search(r"\d+\s+flights found", body, re.I)
                or (
                    re.search(r"(?i)Returning\s+to\b", body)
                    and re.search(r"(?i)\bSelect\b", body)
                    and re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", body)
                    and not loading
                )
            )
            if has_list and not loading:
                # Prefer real parsed rows; keep waiting briefly if only shell markers
                if rows:
                    break
                if re.search(r"\d+\s+flights found", body, re.I):
                    break
        else:
            # One more scroll/settle attempt for late hydration
            try:
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(2500)
                body = page.inner_text("body")
                rows = parse_trip_com_flight_rows(
                    body[:25000], origin=origin, destination=destination
                )
            except Exception:
                pass

        if not rows:
            rows = parse_trip_com_flight_rows(
                body[:25000], origin=origin, destination=destination
            )

        # Prefer inbound rows (arrive back at trip origin / leave destination city)
        out_from = (outbound.get("depart_airport") or "").upper()
        filtered = [
            r
            for r in rows
            if (r.get("arrive_airport") or "").upper() == destination.upper()
            or (
                out_from
                and (r.get("depart_airport") or "").upper() != out_from
            )
        ] or rows
        picked = self._pick_flight_row(filtered)
        if not picked:
            return {}
        if picked.get("airline") and not picked.get("airline_logo"):
            picked["airline_logo"] = airline_logo_url(picked["airline"])
        return picked

    def _scrape_top_flight_card(
        self,
        *,
        origin: str = "",
        destination: str = "",
        lowest: float | None = None,
    ) -> dict[str, str]:
        """Best-effort parse of the first visible flight result on the current page."""
        page = self._require_page()
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        blob = body[:20000]
        card: dict[str, str] = {
            "airline": "",
            "depart_time": "",
            "arrive_time": "",
            "depart_airport": origin or "",
            "arrive_airport": destination or "",
            "duration": "",
            "stops": "Direct",
            "price_label": "",
            "airline_logo": "",
        }

        rows = parse_trip_com_flight_rows(
            blob, origin=origin, destination=destination
        )
        self.last_flight_candidates = [dict(r) for r in rows[:12]]
        picked = self._pick_flight_row(rows, lowest=lowest)
        if picked:
            card.update({k: v for k, v in picked.items() if v})
            if lowest is not None and not card.get("price_label"):
                card["price_label"] = f"HK${lowest:,.0f}"
            if card.get("airline") and not card.get("airline_logo"):
                card["airline_logo"] = airline_logo_url(card["airline"])
            return card

        airline = self._extract_airline_from_text(blob)
        if not airline:
            airline = self._extract_airline_from_dom()
        if airline and not self._is_alliance_or_junk(airline):
            card["airline"] = airline

        # Prefer times near the first priced result block (skip filter sidebar)
        result_chunk = blob
        for m in re.finditer(r"(?i)HK\s*\$\s*[0-9,]+", blob):
            start = max(0, m.start() - 500)
            end = min(len(blob), m.end() + 240)
            candidate = blob[start:end]
            if re.search(r"\d+\s+flights found", candidate, re.I):
                result_chunk = candidate
                break
            if "flights found" in blob.lower():
                idx = blob.lower().find("flights found")
                result_chunk = blob[idx:]
                break

        times = [
            f"{int(h):02d}:{mm}"
            for h, mm in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", result_chunk)
            if f"{int(h):02d}:{mm}" not in {"00:00", "24:00"}
        ]
        if len(times) < 2:
            times = [
                f"{int(h):02d}:{mm}"
                for h, mm in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", blob)
                if f"{int(h):02d}:{mm}" not in {"00:00", "24:00"}
            ]
        if len(times) >= 2:
            card["depart_time"] = times[0]
            card["arrive_time"] = times[1]
        else:
            dep, arr = self._scrape_flight_times_from_dom()
            if dep and arr:
                card["depart_time"] = dep
                card["arrive_time"] = arr

        dur_m = re.search(
            r"\b(\d+\s*h(?:ours?)?(?:\s*\d+\s*m(?:ins?)?)?|\d+h\s*\d+m)\b",
            result_chunk or blob,
            re.I,
        )
        if dur_m and "night" not in dur_m.group(1).lower():
            card["duration"] = re.sub(r"\s+", " ", dur_m.group(1)).strip()

        if re.search(r"(?i)\b\d+\s*stop", result_chunk or blob):
            stop_m = re.search(r"(?i)(\d+)\s*stop", result_chunk or blob)
            card["stops"] = f"{stop_m.group(1)} stop" if stop_m else "1 stop"
        elif re.search(r"(?i)\bdirect\b|\bnon[- ]?stop\b", result_chunk or blob):
            card["stops"] = "Direct"

        if lowest is not None:
            card["price_label"] = f"HK${lowest:,.0f}"
        elif not card.get("price_label"):
            prices = [p for p in parse_prices(result_chunk or blob) if p >= 200]
            if prices:
                card["price_label"] = f"HK${min(prices):,.0f}"

        if card.get("airline") and not card.get("airline_logo"):
            card["airline_logo"] = airline_logo_url(card["airline"])

        return card

    def _extract_airline_from_dom(self) -> str:
        """Read airline name from logos / labels on the flight results page."""
        page = self._require_page()
        candidates: list[str] = []

        # Airline-specific DOM first (avoid QR codes / generic icons)
        for sel in (
            "[class*='airline' i] img[alt]",
            "[class*='Airline' i] img[alt]",
            "[class*='carrier' i] img[alt]",
            "[class*='airline' i]",
            "[class*='Airline' i]",
            "[aria-label*='Airlines' i]",
        ):
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 25)
            except Exception:
                continue
            for i in range(count):
                try:
                    el = loc.nth(i)
                    for attr in ("alt", "title", "aria-label"):
                        raw = (el.get_attribute(attr) or "").strip()
                        name = self._normalize_airline_name(raw)
                        if name:
                            candidates.append(name)
                    text = (el.inner_text(timeout=300) or "").strip()
                    name = self._normalize_airline_name(text)
                    if name:
                        candidates.append(name)
                except Exception:
                    continue

        for name in candidates:
            if is_plausible_airline_name(name):
                return name
        return ""

    def _extract_airline_from_text(self, blob: str) -> str:
        """Fallback airline detection from visible page text / flight codes."""
        airline_m = re.search(
            r"(?i)\b("
            r"Greater Bay Airlines|Cathay Pacific|Hong Kong Airlines|China Airlines|"
            r"EVA Air|Japan Airlines|All Nippon Airways|All Nippon|ANA|"
            r"Singapore Airlines|Thai Airways|Thai AirAsia|AirAsia|"
            r"Korean Air|Asiana|Peach|Scoot|Jetstar|Emirates|Qatar Airways|"
            r"Air France|KLM|Lufthansa|British Airways|Finnair|Turkish Airlines|"
            r"Air China|China Eastern|China Southern|Hainan Airlines|HK Express|"
            r"Virgin Atlantic|Etihad|Swiss International|Swiss|Austrian|"
            r"Iberia|Delta Air Lines|Delta|United Airlines|American Airlines|"
            r"Qantas|Vietnam Airlines|Philippine Airlines|Malaysia Airlines|"
            r"Cebu Pacific"
            r")\b",
            blob or "",
        )
        if airline_m:
            name = self._normalize_airline_name(airline_m.group(1)) or airline_m.group(1)
            if is_plausible_airline_name(name):
                return name

        # Flight number like CX 880 / AF187
        code_map = {
            "CX": "Cathay Pacific",
            "KA": "Hong Kong Airlines",
            "HX": "Hong Kong Airlines",
            "UO": "HK Express",
            "HB": "Greater Bay Airlines",
            "AF": "Air France",
            "KL": "KLM",
            "LH": "Lufthansa",
            "BA": "British Airways",
            "SQ": "Singapore Airlines",
            "NH": "ANA",
            "JL": "Japan Airlines",
            "CI": "China Airlines",
            "BR": "EVA Air",
            "MU": "China Eastern",
            "CZ": "China Southern",
            "CA": "Air China",
            "EK": "Emirates",
            "QR": "Qatar Airways",
            "TK": "Turkish Airlines",
            "AY": "Finnair",
            "TG": "Thai Airways",
            "KE": "Korean Air",
            "OZ": "Asiana",
        }
        code_m = re.search(
            r"\b(" + "|".join(code_map.keys()) + r")\s?-?\s?\d{2,4}\b",
            blob or "",
            re.I,
        )
        if code_m:
            return code_map[code_m.group(1).upper()]
        return ""

    @staticmethod
    def _is_alliance_or_junk(name: str) -> bool:
        return not is_plausible_airline_name(name)

    @staticmethod
    def _normalize_airline_name(raw: str) -> str:
        text = re.sub(r"\s+", " ", (raw or "").strip())
        if not text or len(text) < 2 or len(text) > 60:
            return ""
        expanded = expand_airline_code(text)
        if expanded != text:
            text = expanded
        if not is_plausible_airline_name(text):
            return ""
        return text

    def _scrape_flight_times_from_dom(self) -> tuple[str, str]:
        """Pull the first departure/arrival clock times from result cards."""
        page = self._require_page()
        times: list[str] = []
        selectors = (
            "[class*='time' i]",
            "[class*='Time' i]",
            "[class*='depart' i]",
            "[class*='arrive' i]",
            "[class*='flight' i]",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 40)
            except Exception:
                continue
            for i in range(count):
                try:
                    text = (loc.nth(i).inner_text(timeout=300) or "").strip()
                except Exception:
                    continue
                for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
                    t = f"{int(m.group(1)):02d}:{m.group(2)}"
                    if t in {"00:00", "24:00"}:
                        continue
                    if t not in times:
                        times.append(t)
                if len(times) >= 2:
                    return times[0], times[1]
        return "", ""

    def _enrich_stay_from_picked_hotel(
        self,
        stay_rec: dict[str, str],
        *,
        city: str,
        checkin: str = "",
        checkout: str = "",
    ) -> dict[str, str]:
        """Fill stay name/url/image/score from the LLM-picked hotelId.

        Prefer ``last_hotel_detail_url`` + HTTP/list-card meta for that hotelId.
        Never prefer the first list-card photo over the picked property.
        """
        from travel_agent.trip_urls import (
            canonicalize_hotel_detail_url,
            is_trusted_hotel_detail_url,
        )

        city = (city or stay_rec.get("city") or "").strip()
        detail = (getattr(self, "last_hotel_detail_url", "") or "").strip()
        live_name = (getattr(self, "last_hotel_name", "") or "").strip()
        list_url = (getattr(self, "last_hotel_list_url", "") or "").strip()

        if not detail:
            # Recover a concrete hotelId from candidates / list DOM
            for row in getattr(self, "last_hotel_candidates", None) or []:
                cand = (row.get("url") or "").strip()
                if cand and is_trusted_hotel_detail_url(cand):
                    detail = cand
                    if not live_name and row.get("name"):
                        live_name = row["name"]
                    break
        if not detail:
            try:
                built = self._resolve_top_hotel_detail_url(
                    checkin=checkin or stay_rec.get("checkin") or "",
                    checkout=checkout or stay_rec.get("checkout") or "",
                    adults=2,
                    rooms=1,
                )
            except Exception:
                built = ""
            if built and is_trusted_hotel_detail_url(built):
                detail = built

        if detail and is_trusted_hotel_detail_url(detail):
            detail = (
                canonicalize_hotel_detail_url(
                    detail,
                    checkin=checkin or stay_rec.get("checkin") or "",
                    checkout=checkout or stay_rec.get("checkout") or "",
                    city=city,
                )
                or detail
            )
            self.last_hotel_detail_url = detail
            stay_rec["url"] = detail
            meta = self._scrape_hotel_detail_meta(detail)
            if meta.get("name"):
                cleaned = clean_hotel_display_name(meta["name"], city)
                if cleaned and not cleaned.lower().startswith(
                    ("hotels in ", "recommended hotel")
                ):
                    stay_rec["name"] = cleaned
                    self.last_hotel_name = cleaned
                    live_name = cleaned
            for k in (
                "score",
                "score_label",
                "stars",
                "reviews",
                "location",
                "price_label",
                "total_label",
                "image_url",
            ):
                if meta.get(k):
                    stay_rec[k] = meta[k]
            if not stay_rec.get("image_url"):
                try:
                    img = self.scrape_hotel_image_url(detail)
                except Exception:
                    img = ""
                if img:
                    stay_rec["image_url"] = img
        elif list_url:
            # Last resort: keep list for booking, but still try HTTP on any
            # hotelId we can find in candidates
            low = list_url.lower()
            if "/hotels/list" in low and (
                "city=" in low or "cityid=" in low or "cityname=" in low
            ):
                stay_rec["url"] = ensure_locale_curr(normalize_trip_url(list_url))
            else:
                from travel_agent.trip_urls import build_hotel_list_url

                stay_rec["url"] = build_hotel_list_url(
                    city,
                    checkin or stay_rec.get("checkin") or "",
                    checkout or stay_rec.get("checkout") or "",
                )

        if (
            live_name
            and not live_name.lower().startswith(("hotels in ", "recommended hotel"))
            and _hotel_name_plausible_for_city(live_name, city)
        ):
            if (
                not stay_rec.get("name")
                or stay_rec["name"].lower().startswith(
                    ("hotels in ", "recommended hotel")
                )
                or _is_curated_fallback_name(stay_rec.get("name") or "", city)
            ):
                stay_rec["name"] = clean_hotel_display_name(live_name, city)

        return stay_rec

    def _build_regional_stay_hotel(
        self,
        *,
        stay_city: str,
        airport: str,
        nights: int,
        checkin: str,
        checkout: str,
        label: str,
        adults: int = 2,
        stay_index: int = 2,
    ) -> dict[str, str]:
        """Search Trip.com for a regional stay city via Playwright type→Search.

        Clears prior-city ``last_hotel_*`` first so a failed/partial search never
        reuses the previous stay's hotel name or detail URL.
        """
        fb = _fallback_stay_hotel(stay_city)
        stay_rec: dict[str, str] = {
            "city": stay_city,
            "airport": airport,
            "nights": str(nights),
            "checkin": checkin,
            "checkout": checkout,
            "label": label,
            "name": "",
            "url": "",
            "price_label": "",
            "total_label": "",
            "score": "",
            "score_label": "",
            "stars": "",
            "reviews": "",
            "location": stay_city,
            "image_url": "",
            "features": "",
        }
        # Never inherit the previous city's hotel into this stay
        self.last_hotel_detail_url = ""
        self.last_hotel_name = ""
        self.last_hotel_list_url = ""
        self.last_hotel_candidates = []

        h_text = ""
        try:
            h_text = self.search_hotels(
                city=stay_city,
                checkin=checkin,
                checkout=checkout,
                adults=adults,
                rooms=1,
            )
        except Exception:
            stay_rec["name"] = fb.get("name") or f"Hotels in {stay_city}"
            stay_rec["image_url"] = fb.get("image_url") or _city_hotel_fallback_image(
                stay_city
            )
            stay_rec["features"] = fb.get("features", "")
            stay_rec["score"] = fb.get("score", "")
            stay_rec["score_label"] = fb.get("score_label", "")
            stay_rec["stars"] = fb.get("stars", "4")
            stay_rec["_raw_excerpt"] = (
                f"--- Raw hotel excerpt ({stay_city}) ---\n(search failed)"
            )
            return stay_rec

        # Apply picked hotelId detail URL + official name/photo (not first list card)
        self._enrich_stay_from_picked_hotel(
            stay_rec,
            city=stay_city,
            checkin=checkin,
            checkout=checkout,
        )

        from travel_agent.trip_urls import is_trusted_hotel_detail_url

        if not stay_rec.get("url"):
            stay_url = (getattr(self, "last_hotel_list_url", "") or "").strip()
            if not stay_url:
                canon_h = re.search(r"Canonical search URL:\s*(\S+)", h_text or "")
                stay_url = (canon_h.group(1).rstrip(".,;") if canon_h else "") or ""
            if stay_url:
                stay_rec["url"] = ensure_locale_curr(normalize_trip_url(stay_url))

        h_prices = summarize_prices(
            f"Hotels in {stay_city}",
            h_text,
            url=stay_rec.get("url") or "",
        )
        nightly = h_prices.get("lowest_hkd")
        if not stay_rec.get("price_label") and nightly is not None:
            stay_rec["price_label"] = f"HK${nightly:,.0f}"
        if not stay_rec.get("total_label") and nightly is not None:
            stay_total = round(float(nightly) * nights, 2)
            stay_rec["total_label"] = f"Est. stay total: HK${stay_total:,.0f}"

        # Only use curated city stub when we still have no real hotel
        name = (stay_rec.get("name") or "").strip()
        if (
            not name
            or name.lower().startswith(("hotels in ", "recommended hotel"))
            or _is_curated_fallback_name(name, stay_city)
        ):
            if not is_trusted_hotel_detail_url(stay_rec.get("url") or ""):
                curated = (fb.get("name") or "").strip()
                if curated and not curated.lower().startswith("recommended hotel"):
                    stay_rec["name"] = curated
                stay_rec["features"] = fb.get("features", "")
                if not stay_rec.get("score"):
                    stay_rec["score"] = fb.get("score", "")
                    stay_rec["score_label"] = fb.get("score_label", "")
                if not stay_rec.get("stars"):
                    stay_rec["stars"] = fb.get("stars", "4")
            elif not name:
                stay_rec["name"] = f"Hotels in {stay_city}"

        img = (stay_rec.get("image_url") or "").strip()
        if not img or "loremflickr" in img.lower():
            detail = stay_rec.get("url") or ""
            if is_trusted_hotel_detail_url(detail):
                try:
                    img = self.scrape_hotel_image_url(detail) or img
                except Exception:
                    pass
            if not img or "loremflickr" in (img or "").lower():
                try:
                    from travel_agent.attraction_images import lookup_image

                    img = lookup_image(
                        stay_rec.get("name") or stay_city, city=stay_city
                    ) or img
                except Exception:
                    pass
            if not img:
                img = _city_hotel_fallback_image(
                    stay_city, hotel_name=stay_rec.get("name") or ""
                )
            stay_rec["image_url"] = img

        stay_rec["_raw_excerpt"] = (
            f"--- Raw hotel excerpt ({stay_city}) ---\n" + (h_text or "")[:1800]
        )
        return stay_rec

    def scrape_hotel_image_url(self, detail_url: str = "") -> str:
        """Return the main hotel photo URL for a Trip.com hotel.

        Prefer the list-card overview image for the hotelId (detail pages redirect
        to sign-in under automation). Fall back to HTTP detail HTML, then on-page.
        """
        page = self._require_page()
        if detail_url and "trip.com" in detail_url.lower():
            from travel_agent.trip_urls import _hotel_id_from_url

            hid = _hotel_id_from_url(detail_url)
            if hid:
                self._ensure_hotel_list_for_card_scrape()
                meta = self._scrape_hotel_list_card_by_id(hid)
                img = (meta.get("image_url") or "").strip()
                if img.startswith("http"):
                    return img
            http = _http_fetch_hotel_detail_meta(detail_url)
            img = (http.get("image_url") or "").strip()
            if img.startswith("http"):
                return img

        # Prefer Open Graph cover image (only when already on a usable page)
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        if "/account/signin" not in cur:
            try:
                og = page.locator("meta[property='og:image']")
                if og.count():
                    content = (og.first.get_attribute("content") or "").strip()
                    if content.startswith("http") and "tripcdn.com" in content.lower():
                        return _prefer_hotel_photo_url(content)
            except Exception:
                pass

            for sel in (
                "img[alt*='hotel overview' i]",
                "img[alt*='overview picture' i]",
                "img.m-lazyImg__img",
            ):
                try:
                    loc = page.locator(sel)
                    count = min(loc.count(), 8)
                except Exception:
                    continue
                for i in range(count):
                    try:
                        src = (
                            loc.nth(i).get_attribute("src")
                            or loc.nth(i).evaluate("e => e.currentSrc || ''")
                            or ""
                        ).strip()
                    except Exception:
                        continue
                    if src.startswith("http") and (
                        "tripcdn.com" in src.lower() or "ak-d.tripcdn" in src.lower()
                    ):
                        if any(
                            x in src.lower()
                            for x in ("logo", "icon", "avatar", "qrcode")
                        ):
                            continue
                        return _prefer_hotel_photo_url(src)

        return ""

    def _scrape_top_hotel_card(
        self,
        *,
        city: str = "",
        fallback_name: str = "",
        lowest: float | None = None,
    ) -> dict[str, str]:
        """Best-effort parse of the first hotel listing on the current page."""
        page = self._require_page()
        card: dict[str, str] = {
            "name": fallback_name or "",
            "stars": "",
            "score": "",
            "score_label": "",
            "reviews": "",
            "location": city or "",
            "price_label": "",
            "image_url": "",
        }

        # Prefer structured `.list-item` fields (name/score/price/photo)
        first_hid = ""
        try:
            first_hid = page.evaluate(
                """() => {
                  const a = document.querySelector(
                    'a[href*="hotelId="], a[href*="hotelid="]'
                  );
                  if (!a) return '';
                  const m = (a.getAttribute('href') || a.href || '').match(
                    /hotelId=(\\d+)/i
                  );
                  return m ? m[1] : '';
                }"""
            ) or ""
        except Exception:
            first_hid = ""
        if first_hid:
            meta = self._scrape_hotel_list_card_by_id(str(first_hid))
            for k, v in meta.items():
                if v:
                    card[k] = v

        if not card.get("name") or "sample" in card["name"].lower():
            for _url, label in self._extract_hotel_detail_options(limit=8):
                if (
                    label
                    and _hotel_name_plausible_for_city(label, city)
                    and 3 < len(label) < 100
                ):
                    card["name"] = label
                    break

        if not card.get("price_label") and lowest is not None:
            card["price_label"] = f"HK${lowest:,.0f}"

        if not card.get("image_url"):
            card["image_url"] = _city_hotel_fallback_image(
                city, hotel_name=card.get("name") or ""
            )

        if card.get("name"):
            card["name"] = clean_hotel_display_name(card["name"], city)
        if not card["name"] or not _hotel_name_plausible_for_city(card["name"], city):
            card["name"] = f"Hotels in {city}" if city else "Recommended hotel"
        return card

    def _extract_hotel_detail_options(self, limit: int = 8) -> list[tuple[str, str]]:
        """Return (url, hotel_name) pairs from Trip.com hotel list cards."""
        page = self._require_page()
        found: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        try:
            rows = page.evaluate(
                """(limit) => {
                  const out = [];
                  const seen = new Set();
                  const items = Array.from(
                    document.querySelectorAll('.list-item, [class*="list-item"]')
                  );
                  const pushFrom = (root, hrefHint) => {
                    let href = hrefHint || '';
                    if (!href) {
                      const a = root.querySelector(
                        'a[href*="hotelId="], a[href*="hotelid="]'
                      );
                      href = a ? a.href || a.getAttribute('href') || '' : '';
                    }
                    if (!href) return;
                    const m = href.match(/hotelId=(\\d+)/i);
                    if (!m || seen.has(m[1])) return;
                    seen.add(m[1]);
                    const nameEl =
                      root.querySelector('.hotel-title') ||
                      root.querySelector('a.hotelName') ||
                      root.querySelector('.hotelName');
                    let name = nameEl
                      ? (nameEl.innerText || '').trim().split('\\n')[0].trim()
                      : '';
                    if (!name || name.length < 3 || name.length > 120) {
                      name = '';
                    }
                    out.push({ href, name });
                  };
                  for (const item of items) {
                    pushFrom(item, '');
                    if (out.length >= limit) break;
                  }
                  if (out.length < limit) {
                    const links = Array.from(
                      document.querySelectorAll(
                        'a[href*="hotelId="], a[href*="hotelid="]'
                      )
                    );
                    for (const a of links) {
                      const href = a.href || a.getAttribute('href') || '';
                      const m = href.match(/hotelId=(\\d+)/i);
                      if (!m || seen.has(m[1])) continue;
                      const root =
                        a.closest('.list-item') ||
                        a.closest('[class*="list-item"]') ||
                        a.parentElement;
                      pushFrom(root || a, href);
                      if (out.length >= limit) break;
                    }
                  }
                  return out;
                }""",
                limit,
            )
        except Exception:
            rows = []

        for row in rows or []:
            href = normalize_trip_url(str(row.get("href") or ""))
            if not href or "trip.com" not in href.lower():
                continue
            low = href.lower()
            if "/hotels/" not in low or "all-cities" in low:
                continue
            m = re.search(r"hotelId=(\d+)", href, re.I)
            if not m or m.group(1) in seen_ids:
                continue
            seen_ids.add(m.group(1))
            label = clean_hotel_display_name(str(row.get("name") or ""))
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            found.append((ensure_locale_curr(href), label))
            if len(found) >= limit:
                break

        if found:
            return found

        # Fallback: legacy anchor walk
        seen: set[str] = set()
        try:
            anchors = page.locator("a[href]")
            count = min(anchors.count(), 160)
        except Exception:
            return found

        for i in range(count):
            try:
                a = anchors.nth(i)
                href = (a.get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href:
                continue
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            if "trip.com" not in href.lower():
                continue
            href = normalize_trip_url(href)
            low = href.lower()
            if "/hotels/" not in low or "all-cities" in low:
                continue
            if not (
                "hotelid=" in low
                or re.search(r"hotel-detail-\d+", low)
                or re.search(r"/hotels/[^/?]+-\d+", low)
            ):
                continue
            if href in seen:
                continue
            label = ""
            try:
                raw_label = (a.inner_text(timeout=500) or "").strip()
                raw_label = re.sub(r"\s+", " ", raw_label)
                if (
                    raw_label
                    and 3 < len(raw_label) < 90
                    and "http" not in raw_label.lower()
                    and not re.fullmatch(
                        r"(?i)(see details?|book|select|view|check availability|>)+",
                        raw_label,
                    )
                ):
                    label = clean_hotel_display_name(raw_label)
            except Exception:
                label = ""
            seen.add(href)
            found.append((href, label))
            if len(found) >= limit:
                break
        return found

    def _extract_detail_links(
        self,
        kinds: tuple[str, ...] = ("hotel", "hotels", "flight"),
        limit: int = 5,
    ) -> list[str]:
        """Collect Trip.com detail/list links from the current page."""
        page = self._require_page()
        found: list[str] = []
        seen: set[str] = set()
        try:
            anchors = page.locator("a[href]")
            count = min(anchors.count(), 120)
        except Exception:
            return found

        for i in range(count):
            try:
                href = (anchors.nth(i).get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href:
                continue
            if href.startswith("/"):
                href = f"{settings.trip_base_url.rstrip('/')}{href}"
            if "trip.com" not in href.lower():
                continue
            href = normalize_trip_url(href)
            low = href.lower()
            if not any(k in low for k in kinds):
                continue
            # Prefer concrete hotel/flight result pages over bare hubs
            if low.rstrip("/").endswith(("/hotels", "/flights", "/trains")):
                continue
            if href in seen:
                continue
            seen.add(href)
            found.append(href)
            if len(found) >= limit:
                break
        return found

    def _extract_flightish_content(self) -> str:
        page = self._require_page()
        selectors = [
            "[class*='flight']",
            "[class*='itinerary']",
            "[class*='list-item']",
            "[data-testid*='flight']",
            "main",
            "body",
        ]
        return self._extract_list_content(selectors)

    def _extract_list_content(self, selectors: list[str]) -> str:
        page = self._require_page()
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    text = loc.inner_text(timeout=5000)
                    if text and len(text.strip()) > 80:
                        return f"Extracted via selector `{sel}`:\n{text}"
            except Exception:
                continue
        return page.inner_text("body")


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    # Allow comma-separated strings from the model
    parts = re.split(r"[,|;]", str(value))
    return [p.strip() for p in parts if p.strip()]


# Tool schemas exposed to the Ollama model
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_trip_home",
            "description": "Open the Trip.com Hong Kong homepage (flights, hotels, trains).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": (
                "Search flights on Trip.com Hong Kong. Use IATA city/airport codes "
                "when possible (e.g. hkg, tpe, tyo, nrt, sin, bkk, icn)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Departure city/airport code, e.g. hkg",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Arrival city/airport code, e.g. tyo",
                    },
                    "depart_date": {
                        "type": "string",
                        "description": "Departure date YYYY-MM-DD",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Return date YYYY-MM-DD for round trips",
                    },
                    "trip_type": {
                        "type": "string",
                        "description": "oneway or roundtrip",
                        "enum": ["oneway", "roundtrip"],
                    },
                    "adults": {
                        "type": "integer",
                        "description": "Number of adult passengers (1-9)",
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hotels",
            "description": (
                "Search hotels on Trip.com Hong Kong by typing the city into the "
                "hotels hub (Playwright: Where to? → autocomplete → Search). "
                "Returns the live list URL Trip.com builds after the city is selected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City or area, e.g. Tokyo, Osaka, Taipei",
                    },
                    "checkin": {"type": "string", "description": "Check-in YYYY-MM-DD"},
                    "checkout": {"type": "string", "description": "Check-out YYYY-MM-DD"},
                    "adults": {"type": "integer", "description": "Number of adults"},
                    "rooms": {"type": "integer", "description": "Number of rooms"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_trains",
            "description": (
                "Search train tickets on Trip.com Hong Kong between two stations/cities. "
                "Use as an alternative to flights when ground rail is plausible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "From station/city"},
                    "destination": {"type": "string", "description": "To station/city"},
                    "depart_date": {"type": "string", "description": "Depart YYYY-MM-DD"},
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transfers",
            "description": (
                "Search airport transfers / private ground pickup on Trip.com Hong Kong "
                "for airport↔hotel rides."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or airport area, e.g. Tokyo, Taipei",
                    },
                    "date": {"type": "string", "description": "Service date YYYY-MM-DD"},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_cars",
            "description": (
                "Search car rentals on Trip.com Hong Kong car hire hub "
                "(Playwright: type pickup → autocomplete → Search). "
                "Returns the top deal and a /carrentals/detail booking URL. "
                "Use when rent_car is true or the traveler needs a rental / road trip."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Pick-up city or airport, e.g. Tokyo, Taipei, NRT",
                    },
                    "pickup_date": {
                        "type": "string",
                        "description": "Pick-up date YYYY-MM-DD",
                    },
                    "dropoff_date": {
                        "type": "string",
                        "description": "Drop-off date YYYY-MM-DD",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_flight_prices",
            "description": (
                "Compare flight prices across multiple departure dates and/or alternate "
                "destinations on Trip.com Hong Kong. Use for 'cheapest date' or route comparisons."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure code, e.g. hkg"},
                    "destination": {
                        "type": "string",
                        "description": "Primary destination code, e.g. tpe",
                    },
                    "dates": {
                        "type": "string",
                        "description": (
                            "Comma-separated departure dates YYYY-MM-DD "
                            "(e.g. 2026-08-15,2026-08-20,2026-08-25). Max 4."
                        ),
                    },
                    "trip_type": {
                        "type": "string",
                        "enum": ["oneway", "roundtrip"],
                        "description": "oneway or roundtrip",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Return date YYYY-MM-DD if roundtrip",
                    },
                    "adults": {"type": "integer", "description": "Adult passengers"},
                    "alternate_destinations": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated alternate destination codes "
                            "(e.g. nrt,hnd). Max 2 extras."
                        ),
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_hotel_prices",
            "description": (
                "Compare hotel prices across cities and/or check-in dates on Trip.com Hong Kong."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Primary city, e.g. Taipei"},
                    "checkin": {"type": "string", "description": "Check-in YYYY-MM-DD"},
                    "checkout": {"type": "string", "description": "Check-out YYYY-MM-DD"},
                    "adults": {"type": "integer", "description": "Number of adults"},
                    "rooms": {"type": "integer", "description": "Number of rooms"},
                    "alternate_cities": {
                        "type": "string",
                        "description": "Comma-separated alternate cities (e.g. Taichung,Kaohsiung)",
                    },
                    "alternate_checkins": {
                        "type": "string",
                        "description": (
                            "Comma-separated alternate check-in dates YYYY-MM-DD to compare"
                        ),
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_attractions",
            "description": (
                "Search Trip.com Hong Kong Attractions tab "
                "(https://hk.trip.com/things-to-do/list) for a city, open each "
                "attraction detail page, and return name, photo, address, open "
                "hours, and recommended sightseeing time. Then schedule days "
                "from that list — never return generic 'Attractions & Tours'."
                "Use these exact names in the day-by-day itinerary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City or region, e.g. San Francisco, California, Tokyo",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max attractions to return (default 18)",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_trip_route",
            "description": (
                "REQUIRED FIRST STEP before plan_trip / search_flights / search_hotels. "
                "Propose a rough travel route only (no Trip.com scrape): fly-into airport, "
                "fly-out airport, nights per city, and transfer path. For regions "
                "(California, Florida) this yields multi-city open-jaw plans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "City or region, e.g. Tokyo, California, Florida",
                    },
                    "nights": {
                        "type": "integer",
                        "description": "Total nights for the trip",
                    },
                    "depart_date": {
                        "type": "string",
                        "description": "Outbound date YYYY-MM-DD",
                    },
                    "origin": {
                        "type": "string",
                        "description": "Home city/airport, e.g. Hong Kong",
                    },
                    "arrive_city": {
                        "type": "string",
                        "description": "Optional override: city/airport to fly INTO",
                    },
                    "return_city": {
                        "type": "string",
                        "description": "Optional override: city/airport to fly OUT from",
                    },
                    "stay_cities": {
                        "type": "string",
                        "description": (
                            "Optional stays with nights, e.g. "
                            "'San Francisco:4,Los Angeles:3'"
                        ),
                    },
                    "interests": {
                        "type": "string",
                        "description": "Travel styles, e.g. Food, Culture",
                    },
                },
                "required": ["destination", "nights"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_trip",
            "description": (
                "SECOND STEP after propose_trip_route. Live Trip.com search for flights + "
                "hotels using the proposed arrive/return airports and stay cities. "
                "Pass arrive_airport / return_airport from the rough route."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure airport/city code"},
                    "destination": {
                        "type": "string",
                        "description": "Destination airport/city code or region name",
                    },
                    "depart_date": {"type": "string", "description": "Outbound YYYY-MM-DD"},
                    "return_date": {"type": "string", "description": "Return YYYY-MM-DD"},
                    "adults": {"type": "integer", "description": "Number of adults"},
                    "hotel_city": {
                        "type": "string",
                        "description": "First hotel city (usually first stay city)",
                    },
                    "budget_hkd": {
                        "type": "number",
                        "description": "Optional total budget in HKD",
                    },
                    "interests": {
                        "type": "string",
                        "description": "Trip interests, e.g. food, museums, shopping, road trip",
                    },
                    "rent_car": {
                        "type": "boolean",
                        "description": (
                            "Set true when the traveler needs a rental car / self-drive"
                        ),
                    },
                    "include_flights": {
                        "type": "boolean",
                        "description": "Include flight search (default true)",
                    },
                    "include_trains": {
                        "type": "boolean",
                        "description": "Include train search as an alternative (default true)",
                    },
                    "include_transfers": {
                        "type": "boolean",
                        "description": (
                            "Include airport transfer / ground pickup search (default true)"
                        ),
                    },
                    "arrive_airport": {
                        "type": "string",
                        "description": "IATA to fly INTO from propose_trip_route (e.g. sfo)",
                    },
                    "return_airport": {
                        "type": "string",
                        "description": "IATA to fly OUT from (e.g. lax)",
                    },
                    "stay_cities": {
                        "type": "string",
                        "description": (
                            "Optional stay list, e.g. 'San Francisco:4,Los Angeles:3'"
                        ),
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "Open a specific Trip.com URL and read the visible page content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL or Trip.com path"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page_summary",
            "description": "Read the title, URL, and visible text of the current browser page.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_text",
            "description": "Click the first visible element that contains the given text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Visible text to click"},
                },
                "required": ["text"],
            },
        },
    },
]


def dispatch_tool(browser: TripBrowser, name: str, arguments: dict[str, Any] | str) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments = {}

    handlers = {
        "open_trip_home": lambda: browser.open_home(),
        "search_flights": lambda: browser.search_flights(**arguments),
        "search_hotels": lambda: browser.search_hotels(**arguments),
        "search_trains": lambda: browser.search_trains(**arguments),
        "search_transfers": lambda: browser.search_transfers(**arguments),
        "search_cars": lambda: browser.search_cars(**arguments),
        "compare_flight_prices": lambda: browser.compare_flight_prices(**arguments),
        "compare_hotel_prices": lambda: browser.compare_hotel_prices(**arguments),
        "propose_trip_route": lambda: browser.propose_trip_route(**arguments),
        "plan_trip": lambda: browser.plan_trip(**arguments),
        "search_attractions": lambda: browser.search_attractions(**arguments),
        "browse_url": lambda: browser.browse_url(**arguments),
        "get_page_summary": lambda: browser.get_page_summary(),
        "click_text": lambda: browser.click_text(**arguments),
    }
    handler = handlers.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return str(handler())
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}. Got: {arguments}"
    except Exception as exc:  # noqa: BLE001 — surface browser errors to the model
        return f"Tool `{name}` failed: {type(exc).__name__}: {exc}"
