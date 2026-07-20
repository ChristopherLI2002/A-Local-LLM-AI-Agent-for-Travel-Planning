"""Normalize, build, and rank Trip.com booking links for the UI."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from travel_agent.config import settings
from travel_agent.places import to_flight_code, to_hotel_city, to_hotel_city_id

_TRIP_HOST_RE = re.compile(
    r"^(?:www|hk|us|uk|jp|sg|kr|tw|de|fr|es|it|au|my|th|vn|ph|id)\.trip\.com$",
    re.I,
)
_CANON_RE = re.compile(r"Canonical search URL:\s*(\S+)", re.I)
_FLIGHT_BOOK_RE = re.compile(
    r"(?:Book this flight search|Recommended flight link):\s*(\S+)",
    re.I,
)
_HOTEL_BOOK_RE = re.compile(
    r"(?:Book this hotel search|Recommended hotel list link):\s*(\S+)",
    re.I,
)


def normalize_trip_url(url: str) -> str:
    """Rewrite regional Trip.com hosts to hk.trip.com and strip junk fragments."""
    text = (url or "").strip().rstrip(".,;)")
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    if not text.startswith("http"):
        return text

    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if not _TRIP_HOST_RE.match(host) and "trip.com" not in host:
        return text

    netloc = urlparse(settings.trip_base_url).netloc or "hk.trip.com"
    path = parsed.path or "/"
    query = parsed.query
    return urlunparse(("https", netloc, path, "", query, ""))


def _has_numeric_city(url: str) -> bool:
    qs = parse_qs(urlparse(url).query)
    city = (qs.get("city") or [""])[0]
    return bool(re.fullmatch(r"\d{1,6}", city))


def score_booking_url(url: str, kind: str) -> int:
    """Higher is better. Prefer stable hk.trip.com search/list links."""
    raw = (url or "").strip()
    if not raw or "trip.com" not in raw.lower():
        return -10_000

    norm = normalize_trip_url(raw)
    low = norm.lower()
    score = 0

    if "hk.trip.com" in low:
        score += 40
    elif "trip.com" in low:
        score += 5

    if kind == "flight":
        if "/flights/showfarefirst" in low and "dcity=" in low and "acity=" in low:
            score += 120
        elif "/flights/showfarefirst" in low:
            score += 90
        elif "/flights/" in low and ("dcity=" in low or "acity=" in low):
            score += 70
        elif "/flights/" in low:
            score += 15
        if re.search(r"/tickets-[a-z]{3}-[a-z]{3}/?", low):
            score -= 100
        if "hotel" in low:
            score -= 60
    elif kind == "hotel":
        # Stable list URLs work in any browser; session-bound detail links often fail.
        if "/hotels/list" in low and _has_numeric_city(norm):
            score += 150
        elif "/hotels/list" in low:
            score += 60
        elif re.search(r"hotelid=\d+|hotel-detail-\d+", low):
            if "hoteluniquekey=" in low:
                score += 10
            else:
                score += 70
        elif "/hotels/" in low:
            score += 10
        if "flight" in low:
            score -= 60
        if re.search(r"hotel-detail(?!-\d)", low) and "hotelid=" not in low:
            score -= 80
    else:
        if "hk.trip.com" in low:
            score += 20

    return score


def pick_booking_url(urls: list[str], kind: str, fallback: str = "") -> str:
    """Choose the best flight/hotel booking URL from candidates."""
    candidates = [u for u in urls if u and "trip.com" in u.lower()]
    if fallback and "trip.com" in fallback.lower():
        candidates.append(fallback)
    if not candidates:
        return ensure_locale_curr(fallback) if fallback else ""

    best = max(candidates, key=lambda u: score_booking_url(u, kind))
    if score_booking_url(best, kind) < 0:
        return ensure_locale_curr(fallback) if fallback else ""
    return ensure_locale_curr(normalize_trip_url(best))


def ensure_locale_curr(url: str) -> str:
    """Append locale/curr when missing on hk.trip.com links."""
    norm = normalize_trip_url(url)
    if not norm or "trip.com" not in norm.lower():
        return norm
    parsed = urlparse(norm)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    flat: dict[str, str] = {k: v[0] for k, v in qs.items() if v}
    flat.setdefault("locale", settings.trip_locale)
    flat.setdefault("curr", settings.trip_currency)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(flat), "")
    )


def build_flight_search_url(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
    trip_type: str = "roundtrip",
) -> str:
    """Build a stable hk.trip.com flight results URL from trip parameters."""
    dcity = to_flight_code(origin)
    acity = to_flight_code(destination)
    is_round = trip_type.strip().lower() in {"round", "roundtrip", "rt", "2", "return"}
    params: dict[str, Any] = {
        "dcity": dcity,
        "acity": acity,
        "ddate": depart_date,
        "triptype": "rt" if is_round else "ow",
        "class": "y",
        "quantity": max(1, min(int(adults or 1), 9)),
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
            params["rdate"] = return_date or depart_date
    url = f"{settings.trip_base_url}/flights/showfarefirst?{urlencode(params)}"
    return ensure_locale_curr(url)


def build_hotel_list_url(
    city: str,
    checkin: str,
    checkout: str,
    adults: int = 2,
    rooms: int = 1,
) -> str:
    """Build a stable hk.trip.com hotel list URL from trip parameters."""
    city_name = to_hotel_city(city)
    city_id = to_hotel_city_id(city) or to_hotel_city_id(city_name)
    params: dict[str, Any] = {
        "checkin": checkin,
        "checkout": checkout,
        "adult": max(1, min(int(adults or 2), 8)),
        "crn": max(1, min(int(rooms or 1), 8)),
        "locale": settings.trip_locale,
        "curr": settings.trip_currency,
    }
    if city_id:
        params["city"] = city_id
        params["cityName"] = city_name
    else:
        params["city"] = city_name
        params["cityName"] = city_name
        params["keyword"] = city_name
    url = f"{settings.trip_base_url}/hotels/list?{urlencode(params)}"
    return ensure_locale_curr(url)


def extract_booking_urls(text: str) -> dict[str, str]:
    """Pull flight/hotel booking URLs from plan_trip or search tool output."""
    out = {"flight": "", "hotel": ""}
    if not text:
        return out

    for m in _CANON_RE.finditer(text):
        url = ensure_locale_curr(m.group(1).rstrip(".,;"))
        low = url.lower()
        if "/flights/" in low and score_booking_url(url, "flight") > score_booking_url(
            out["flight"], "flight"
        ):
            out["flight"] = url
        if "/hotels/" in low and score_booking_url(url, "hotel") > score_booking_url(
            out["hotel"], "hotel"
        ):
            out["hotel"] = url

    for m in _FLIGHT_BOOK_RE.finditer(text):
        url = ensure_locale_curr(m.group(1).rstrip(".,;"))
        if score_booking_url(url, "flight") >= score_booking_url(out["flight"], "flight"):
            out["flight"] = url

    for m in _HOTEL_BOOK_RE.finditer(text):
        url = ensure_locale_curr(m.group(1).rstrip(".,;"))
        if score_booking_url(url, "hotel") >= score_booking_url(out["hotel"], "hotel"):
            out["hotel"] = url

    return out


def resolve_booking_url(
    kind: str,
    *,
    tool_url: str = "",
    built_url: str = "",
    parsed_url: str = "",
) -> str:
    """Pick the best URL: tool output > built search URL > parsed LLM text."""
    tool_url = ensure_locale_curr(tool_url) if tool_url else ""
    built_url = ensure_locale_curr(built_url) if built_url else ""
    parsed_url = ensure_locale_curr(parsed_url) if parsed_url else ""

    if kind == "hotel" and built_url and "/hotels/list" in built_url.lower():
        if not _has_numeric_city(built_url):
            built_url = ""

    if tool_url and score_booking_url(tool_url, kind) >= 50:
        return tool_url
    if built_url:
        return built_url
    best = pick_booking_url(
        [u for u in (tool_url, parsed_url) if u],
        kind,
        fallback=built_url,
    )
    return best or built_url
