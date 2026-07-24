"""Normalize, build, and rank Trip.com booking links for the UI."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from travel_agent.config import settings
from travel_agent.places import to_flight_code, to_hotel_city
from travel_agent.airline_names import is_plausible_airline_name, airline_logo_url

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
_HOTEL_DETAIL_RE = re.compile(
    r"(?:Hotel option link|Recommended hotel detail link|Hotel detail link):\s*(\S+)",
    re.I,
)
_HOTEL_NAME_RE = re.compile(
    r"(?:Recommended hotel name|Hotel name)\s*:\s*(.+)$",
    re.I | re.M,
)
_FAKE_HOTEL_IDS = frozenset(
    {
        "123456",
        "1234567",
        "12345678",
        "999999",
        "000000",
        "111111",
    }
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


def _qs(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}


def _hotel_id_from_url(url: str) -> str:
    qs = _qs(url)
    for key in ("hotelId", "hotelid", "masterhotelid"):
        val = (qs.get(key) or "").strip()
        if re.fullmatch(r"\d{4,10}", val):
            return val
    m = re.search(r"hotel-detail-(\d+)", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/hotels/[^/?]+-(\d+)", url, re.I)
    if m:
        return m.group(1)
    return ""


def is_hotel_detail_url(url: str) -> bool:
    low = (url or "").lower()
    if not low or "trip.com" not in low:
        return False
    if "/hotels/detail" in low:
        return True
    return bool(_hotel_id_from_url(url))


def is_hotel_list_url(url: str) -> bool:
    """True for hk.trip.com hotel list/search pages."""
    low = (url or "").lower()
    if not low or "trip.com" not in low:
        return False
    if "/hotels/list" in low:
        return True
    if "/hotels/" in low and "checkin" in low and (
        "city=" in low or "destname=" in low or "keyword=" in low or "cityname=" in low
    ):
        return True
    return False


def is_trusted_hotel_detail_url(url: str) -> bool:
    """True for real hotel detail pages with a plausible numeric hotelId."""
    if not is_hotel_detail_url(url):
        return False
    hid = _hotel_id_from_url(url)
    if not hid or hid in _FAKE_HOTEL_IDS:
        return False
    return True


def is_openable_hotel_url(url: str) -> bool:
    """Detail page or city hotel list — both OK for Check Availability."""
    return is_trusted_hotel_detail_url(url) or is_hotel_list_url(url)


def canonicalize_hotel_detail_url(
    url: str,
    *,
    checkin: str = "",
    checkout: str = "",
    city: str = "",
) -> str:
    """Normalize a hotel detail URL and patch missing dates/locale/currency."""
    if not url:
        return ""
    norm = normalize_trip_url(url)
    if not is_hotel_detail_url(norm):
        return ensure_locale_curr(norm)

    qs = _qs(norm)
    hid = _hotel_id_from_url(norm)
    if hid:
        qs["hotelId"] = hid
    for old in ("hotelid", "masterhotelid"):
        qs.pop(old, None)

    if checkin:
        qs["checkIn"] = checkin
        qs.pop("checkin", None)
    elif "checkin" in qs and "checkIn" not in qs:
        qs["checkIn"] = qs.pop("checkin")

    if checkout:
        qs["checkOut"] = checkout
        qs.pop("checkout", None)
    elif "checkout" in qs and "checkOut" not in qs:
        qs["checkOut"] = qs.pop("checkout")

    if city and not qs.get("cityId"):
        # Do not inject hardcoded city IDs — Trip.com detail links already
        # identify the property; optional cityEnName is display-only.
        if city:
            qs.setdefault("cityEnName", to_hotel_city(city) or city)

    qs.setdefault("adult", "2")
    qs.setdefault("children", "0")
    qs.setdefault("crn", "1")
    qs.setdefault("ages", "")
    qs.setdefault("curr", settings.trip_currency)
    qs.setdefault("barcurr", settings.trip_currency)
    qs.setdefault("locale", settings.trip_locale)

    parsed = urlparse(norm)
    path = parsed.path if "/hotels/detail" in parsed.path else "/hotels/detail/"
    return urlunparse(
        ("https", urlparse(settings.trip_base_url).netloc or "hk.trip.com", path, "", urlencode(qs), "")
    )


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
        if is_trusted_hotel_detail_url(norm):
            score += 220
            if "checkin=" in low:
                score += 40
            if "cityid=" in low:
                score += 20
        elif re.search(r"hotelid=\d+|hotel-detail-\d+", low):
            score += 40
        elif "/hotels/list" in low and _has_numeric_city(norm):
            score += 25
        elif "/hotels/list" in low:
            score += 10
        elif "/hotels/" in low:
            score += 5
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
    if kind == "hotel":
        trusted = [u for u in candidates if is_trusted_hotel_detail_url(u)]
        if fallback and is_trusted_hotel_detail_url(fallback):
            trusted.append(fallback)
        if trusted:
            best = max(trusted, key=lambda u: score_booking_url(u, kind))
            return _finalize(kind, best)
        return _finalize(kind, fallback) if is_trusted_hotel_detail_url(fallback) else ""
    if fallback and "trip.com" in fallback.lower():
        candidates.append(fallback)
    if not candidates:
        return ensure_locale_curr(fallback) if fallback else ""

    best = max(candidates, key=lambda u: score_booking_url(u, kind))
    if score_booking_url(best, kind) < 0:
        return ensure_locale_curr(fallback) if fallback else ""
    return _finalize(kind, best)


def _finalize(kind: str, url: str) -> str:
    if not url:
        return ""
    if kind == "hotel" and is_hotel_detail_url(url):
        cleaned = canonicalize_hotel_detail_url(url)
        return cleaned or ensure_locale_curr(normalize_trip_url(url))
    return ensure_locale_curr(normalize_trip_url(url))


def ensure_locale_curr(url: str) -> str:
    """Append locale/curr when missing on hk.trip.com links."""
    norm = normalize_trip_url(url)
    if not norm or "trip.com" not in norm.lower():
        return norm
    if is_hotel_detail_url(norm):
        cleaned = canonicalize_hotel_detail_url(norm)
        return cleaned or norm
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
    """Build a Trip.com hotel search link without hardcoded city IDs.

    Prefer the live URL returned by Playwright hub search when available.
    This fallback uses keyword + cityName so Check Availability still opens
    a usable search (Trip.com resolves the destination itself).
    """
    city_name = to_hotel_city(city) or (city or "").strip() or "Destination"
    params: dict[str, Any] = {
        "cityName": city_name,
        "keyword": city_name,
        "checkin": checkin,
        "checkout": checkout,
        "adult": max(1, min(int(adults or 2), 8)),
        "crn": max(1, min(int(rooms or 1), 8)),
        "locale": settings.trip_locale,
        "curr": settings.trip_currency,
    }
    url = f"{settings.trip_base_url}/hotels/list?{urlencode(params)}"
    return ensure_locale_curr(url)


def extract_booking_urls(text: str) -> dict[str, str]:
    """Pull flight/hotel booking URLs from plan_trip or search tool output."""
    out = {"flight": "", "hotel": "", "hotel_name": ""}
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

    for m in _HOTEL_DETAIL_RE.finditer(text):
        url = _finalize("hotel", m.group(1).rstrip(".,;"))
        if url and score_booking_url(url, "hotel") >= score_booking_url(
            out["hotel"], "hotel"
        ):
            out["hotel"] = url

    name_m = _HOTEL_NAME_RE.search(text)
    if name_m:
        name = name_m.group(1).strip().strip("·-|")
        if (
            name
            and "http" not in name.lower()
            and len(name) < 90
            and "sample hotel" not in name.lower()
            and "rates range" not in name.lower()
        ):
            out["hotel_name"] = name

    # Structured cards emitted by search_flights / search_hotels
    flight_card = re.search(
        r"(?is)Structured flight card:\s*(.*?)(?:\n\n|Structured hotel|Canonical|City/keyword|Hotel search|Flight search|$)",
        text,
    )
    if flight_card:
        block = flight_card.group(1)
        for key, dest in (
            (r"(?im)^-\s*Airline:\s*(.+)$", "flight_airline"),
            (r"(?im)^-\s*Airline logo:\s*(\S+)", "flight_airline_logo"),
            (r"(?im)^-\s*Date:\s*(\d{4}-\d{2}-\d{2})", "flight_date"),
            (r"(?im)^-\s*Depart:\s*([0-2]?\d:[0-5]\d)", "flight_depart"),
            (r"(?im)^-\s*Arrive:\s*([0-2]?\d:[0-5]\d)", "flight_arrive"),
            (r"(?im)^-\s*From:\s*(.+)$", "flight_from"),
            (r"(?im)^-\s*To:\s*(.+)$", "flight_to"),
            (r"(?im)^-\s*Duration:\s*(.+)$", "flight_duration"),
            (r"(?im)^-\s*Stops:\s*(.+)$", "flight_stops"),
            (r"(?im)^-\s*Price:\s*(.+)$", "flight_price"),
            (r"(?im)^-\s*Return airline:\s*(.+)$", "flight_return_airline"),
            (r"(?im)^-\s*Return airline logo:\s*(\S+)", "flight_return_airline_logo"),
            (r"(?im)^-\s*Return date:\s*(\d{4}-\d{2}-\d{2})", "flight_return_date"),
            (r"(?im)^-\s*Return depart:\s*([0-2]?\d:[0-5]\d)", "flight_return_depart"),
            (r"(?im)^-\s*Return arrive:\s*([0-2]?\d:[0-5]\d)", "flight_return_arrive"),
            (r"(?im)^-\s*Return from:\s*(.+)$", "flight_return_from"),
            (r"(?im)^-\s*Return to:\s*(.+)$", "flight_return_to"),
            (r"(?im)^-\s*Return duration:\s*(.+)$", "flight_return_duration"),
            (r"(?im)^-\s*Return stops:\s*(.+)$", "flight_return_stops"),
        ):
            m = re.search(key, block)
            if m:
                val = m.group(1).strip()
                if dest in {"flight_airline", "flight_return_airline"} and not is_plausible_airline_name(val):
                    continue
                if val and "night" not in val.lower():
                    out[dest] = val

    hotel_card = re.search(
        r"(?is)Structured hotel card:\s*(.*?)(?:\n\n|City/keyword|Canonical|Hotel search|Structured flight|$)",
        text,
    )
    if hotel_card:
        block = hotel_card.group(1)
        for key, dest in (
            (r"(?im)^-\s*Hotel:\s*(.+)$", "hotel_name"),
            (r"(?im)^-\s*Stars:\s*(\d)", "hotel_stars"),
            (r"(?im)^-\s*Score:\s*(.+)$", "hotel_score"),
            (r"(?im)^-\s*Location:\s*(.+)$", "hotel_location"),
            (r"(?im)^-\s*Nightly:\s*(.+)$", "hotel_price"),
            (r"(?im)^-\s*Reviews:\s*(.+)$", "hotel_reviews"),
            (r"(?im)^-\s*Image:\s*(\S+)", "hotel_image"),
        ):
            m = re.search(key, block)
            if m:
                val = m.group(1).strip()
                if dest == "hotel_name" and (
                    "sample hotel" in val.lower() or "rates range" in val.lower()
                ):
                    continue
                if val:
                    out[dest] = val

    fp = re.search(r"(?i)Lowest seen[^\n]*?HK\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)", text)
    if fp and not out.get("flight_price"):
        out["flight_price"] = f"HK${fp.group(1)}"
    # Prefer Option seen under RECOMMENDED FLIGHT (first match before hotel section)
    flight_section = re.search(
        r"(?is)RECOMMENDED FLIGHT\b(.*?)(?:RECOMMENDED HOTEL\b|$)",
        text,
    )
    if flight_section:
        block = flight_section.group(1)
        fopt = re.search(r"(?im)^-\s*Option seen:\s*(.+)$", block)
        if fopt:
            out["flight_option"] = fopt.group(1).strip()[:160]
        for key, dest in (
            (r"(?im)^-\s*Airline:\s*(.+)$", "flight_airline"),
            (r"(?im)^-\s*Airline logo:\s*(\S+)", "flight_airline_logo"),
            (r"(?im)^-\s*Date:\s*(\d{4}-\d{2}-\d{2})", "flight_date"),
            (r"(?im)^-\s*Depart:\s*([0-2]?\d:[0-5]\d)", "flight_depart"),
            (r"(?im)^-\s*Arrive:\s*([0-2]?\d:[0-5]\d)", "flight_arrive"),
            (r"(?im)^-\s*From:\s*(.+)$", "flight_from"),
            (r"(?im)^-\s*To:\s*(.+)$", "flight_to"),
            (r"(?im)^-\s*Duration:\s*(.+)$", "flight_duration"),
            (r"(?im)^-\s*Stops:\s*(.+)$", "flight_stops"),
            (r"(?im)^-\s*Return airline:\s*(.+)$", "flight_return_airline"),
            (r"(?im)^-\s*Return airline logo:\s*(\S+)", "flight_return_airline_logo"),
            (r"(?im)^-\s*Return date:\s*(\d{4}-\d{2}-\d{2})", "flight_return_date"),
            (r"(?im)^-\s*Return depart:\s*([0-2]?\d:[0-5]\d)", "flight_return_depart"),
            (r"(?im)^-\s*Return arrive:\s*([0-2]?\d:[0-5]\d)", "flight_return_arrive"),
            (r"(?im)^-\s*Return from:\s*(.+)$", "flight_return_from"),
            (r"(?im)^-\s*Return to:\s*(.+)$", "flight_return_to"),
            (r"(?im)^-\s*Return duration:\s*(.+)$", "flight_return_duration"),
            (r"(?im)^-\s*Return stops:\s*(.+)$", "flight_return_stops"),
        ):
            if out.get(dest):
                continue
            m = re.search(key, block)
            if not m:
                continue
            val = m.group(1).strip()
            if dest in {"flight_airline", "flight_return_airline"} and not is_plausible_airline_name(
                val
            ):
                continue
            if val and "night" not in val.lower():
                out[dest] = val

    hotel_section = re.search(
        r"(?is)RECOMMENDED HOTEL\b(.*?)(?:ALTERNATIVE|Other Trip\.com|=======|$)",
        text,
    )
    if hotel_section:
        hp = re.search(
            r"(?i)Lowest nightly[^\n]*?HK\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)",
            hotel_section.group(1),
        )
        if hp and not out.get("hotel_price"):
            out["hotel_price"] = f"HK${hp.group(1)}"
        ht = re.search(
            r"(?i)Est\.?\s*stay total[^\n]*?HK\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)",
            hotel_section.group(1),
        )
        if ht:
            out["hotel_total"] = f"HK${ht.group(1)}"
        if not out.get("hotel_name"):
            hopt = re.search(r"(?im)^-\s*Option seen:\s*(.+)$", hotel_section.group(1))
            if hopt:
                cand = hopt.group(1).strip()
                if cand and "http" not in cand.lower() and "sample" not in cand.lower():
                    # Often "Name — HK$..." style snippets
                    name_part = re.split(r"\s+[—\-]\s+|:\s*", cand)[0].strip()
                    if (
                        3 < len(name_part) < 90
                        and "rate" not in name_part.lower()
                        and "sample" not in name_part.lower()
                    ):
                        out["hotel_name"] = name_part

    if out["hotel"] and not is_trusted_hotel_detail_url(out["hotel"]):
        out["hotel"] = ""

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

    if kind == "hotel":
        for cand in (tool_url, parsed_url):
            if is_trusted_hotel_detail_url(cand):
                return _finalize(
                    "hotel",
                    canonicalize_hotel_detail_url(cand) or cand,
                )
        best = pick_booking_url(
            [u for u in (tool_url, parsed_url, built_url) if u],
            "hotel",
            fallback=built_url,
        )
        if best:
            return best
        return built_url

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


def fetch_hotel_detail_link(
    browser: object,
    *,
    city: str,
    checkin: str,
    checkout: str,
    adults: int = 2,
) -> dict[str, str]:
    """Scrape a trusted hotel detail URL from Trip.com (must run on browser thread)."""
    search = getattr(browser, "search_hotels", None)
    if not search:
        return {"url": "", "name": ""}

    text = search(city, checkin=checkin, checkout=checkout, adults=adults)
    found = extract_booking_urls(text)
    url = found.get("hotel", "")
    if not is_trusted_hotel_detail_url(url):
        for m in _HOTEL_DETAIL_RE.finditer(text or ""):
            cand = _finalize("hotel", m.group(1).rstrip(".,;"))
            if is_trusted_hotel_detail_url(cand):
                url = cand
                break
    if url:
        url = canonicalize_hotel_detail_url(
            url, checkin=checkin, checkout=checkout, city=city
        )

    name = found.get("hotel_name", "")
    if not name or "sample" in name.lower() or "rates range" in name.lower():
        name_m = _HOTEL_NAME_RE.search(text or "")
        if name_m:
            candidate = name_m.group(1).strip()
            if (
                candidate
                and "http" not in candidate.lower()
                and len(candidate) < 90
                and "sample" not in candidate.lower()
            ):
                name = candidate

    out = {"url": url, "name": name}
    for key in (
        "hotel_price",
        "hotel_total",
        "hotel_stars",
        "hotel_score",
        "hotel_location",
        "hotel_reviews",
        "hotel_image",
    ):
        if found.get(key):
            out[key] = found[key]

    # Always try the detail page cover photo when we have a trusted hotel URL
    if url and is_trusted_hotel_detail_url(url) and not out.get("hotel_image"):
        scrape_img = getattr(browser, "scrape_hotel_image_url", None)
        if scrape_img:
            try:
                img = scrape_img(url)
            except Exception:
                img = ""
            if img:
                out["hotel_image"] = img
    return out


def fetch_flight_card(
    browser: object,
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str = "",
    adults: int = 1,
) -> dict[str, str]:
    """Scrape airline + times for outbound (and return) flights (browser thread)."""
    search = getattr(browser, "search_flights", None)
    if not search:
        return {}

    text = search(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date or None,
        trip_type="roundtrip" if return_date else "oneway",
        adults=adults,
    )
    found = extract_booking_urls(text)
    out: dict[str, str] = {}
    url = found.get("flight") or ""
    if url:
        out["flight"] = url
    for key in (
        "flight_airline",
        "flight_depart",
        "flight_arrive",
        "flight_from",
        "flight_to",
        "flight_duration",
        "flight_stops",
        "flight_price",
        "flight_airline_logo",
        "flight_date",
        "flight_return_airline",
        "flight_return_depart",
        "flight_return_arrive",
        "flight_return_from",
        "flight_return_to",
        "flight_return_duration",
        "flight_return_stops",
        "flight_return_airline_logo",
        "flight_return_date",
    ):
        if found.get(key):
            val = found[key]
            if key in {"flight_airline", "flight_return_airline"} and not is_plausible_airline_name(
                val
            ):
                continue
            out[key] = val
    if out.get("flight_airline") and not out.get("flight_airline_logo"):
        out["flight_airline_logo"] = airline_logo_url(out["flight_airline"])
    if out.get("flight_return_airline") and not out.get("flight_return_airline_logo"):
        out["flight_return_airline_logo"] = airline_logo_url(out["flight_return_airline"])
    return out
