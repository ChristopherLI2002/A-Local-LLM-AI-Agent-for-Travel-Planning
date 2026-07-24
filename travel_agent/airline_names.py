"""Heuristics for recognizing real airline names vs Trip.com UI junk."""

from __future__ import annotations

import re

_AIRLINE_RE = re.compile(
    r"(?i)\b("
    r"Greater Bay Airlines|Cathay Pacific|Hong Kong Airlines|China Airlines|"
    r"EVA Air|Japan Airlines|All Nippon Airways|All Nippon|ANA|"
    r"Singapore Airlines|Thai Airways|Thai AirAsia|AirAsia|"
    r"Korean Air|Asiana|Peach|Scoot|Jetstar|Emirates|Qatar Airways|"
    r"Air France|KLM|Lufthansa|British Airways|Finnair|Turkish Airlines|"
    r"Air China|China Eastern|China Southern|Hainan Airlines|HK Express|"
    r"Virgin Atlantic|Etihad|Swiss|Austrian|Iberia|Delta|"
    r"United Airlines|American Airlines|Delta Air Lines|Delta Airlines|"
    r"Alaska Airlines|Southwest Airlines|JetBlue|Hawaiian Airlines|Air Canada|"
    r"Qantas|Vietnam Airlines|"
    r"Philippine Airlines|Malaysia Airlines|Cebu Pacific|Etihad Airways|SWISS|"
    r"KLM Royal Dutch Airlines|"
    r"CX|HB|UO|AF|KL|SQ|NH|JL|CI|BR|MU|CZ|CA|EK|QR|TK|AY|TG|KE|OZ|"
    r"UA|AA|DL|AS|WN|B6|HA|AC"
    r")\b"
)

_JUNK_AIRLINE_SUBSTR = (
    "qrcode",
    "qr code",
    "barcode",
    "oneworld",
    "one world",
    "star alliance",
    "skyteam",
    "sky team",
    "trip.com",
    "tripcom",
    "alliance",
    "codeshare",
    "operated by",
    "logo",
    "icon",
    "image",
    "photo",
    "button",
    "share",
    "download",
    "scan",
    "widget",
    "placeholder",
    "loading",
    "menu",
    "close",
    "search",
    "filter",
    "sort",
    "select",
    "book",
)


def is_plausible_airline_name(name: str) -> bool:
    """True when text looks like a real airline, not UI/alliance junk."""
    text = (name or "").strip()
    if not text or len(text) < 2 or len(text) > 60:
        return False
    low = text.lower()
    if any(j in low for j in _JUNK_AIRLINE_SUBSTR):
        return False
    if _AIRLINE_RE.search(text):
        return True
    if re.search(r"(?i)\b(airlines?|airways)\b", text):
        return True
    return False


# IATA / logo alt codes seen on Trip.com result rows
AIRLINE_CODE_MAP: dict[str, str] = {
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
    "MU": "China Eastern Airlines",
    "CZ": "China Southern",
    "CA": "Air China",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "TK": "Turkish Airlines",
    "AY": "Finnair",
    "TG": "Thai Airways",
    "KE": "Korean Air",
    "OZ": "Asiana",
    "EY": "Etihad Airways",
    "LX": "SWISS",
    "VN": "Vietnam Airlines",
    "MH": "Malaysia Airlines",
    "UA": "United Airlines",
    "AA": "American Airlines",
    "DL": "Delta Air Lines",
    "AS": "Alaska Airlines",
    "WN": "Southwest Airlines",
    "B6": "JetBlue",
    "F9": "Frontier Airlines",
    "NK": "Spirit Airlines",
    "HA": "Hawaiian Airlines",
    "AC": "Air Canada",
}


def expand_airline_code(name: str) -> str:
    """Map 2-letter logo alt codes to full airline names."""
    text = (name or "").strip()
    if not text:
        return ""
    if len(text) == 2 and text.upper() in AIRLINE_CODE_MAP:
        return AIRLINE_CODE_MAP[text.upper()]
    return text


_AIRLINE_NAME_ALIASES: dict[str, str] = {
    "etihad airways": "EY",
    "etihad": "EY",
    "swiss": "LX",
    "swiss international air lines": "LX",
    "klm royal dutch airlines": "KL",
    "china eastern airlines": "MU",
    "china southern airlines": "CZ",
    "air china": "CA",
    "turkish airlines": "TK",
    "qatar airways": "QR",
    "singapore airlines": "SQ",
    "cathay pacific": "CX",
    "air france": "AF",
    "british airways": "BA",
    "lufthansa": "LH",
    "eva air": "BR",
    "ana": "NH",
    "all nippon airways": "NH",
    "all nippon": "NH",
    "japan airlines": "JL",
    "korean air": "KE",
    "asiana": "OZ",
    "emirates": "EK",
    "finnair": "AY",
    "thai airways": "TG",
    "vietnam airlines": "VN",
    "malaysia airlines": "MH",
    "greater bay airlines": "HB",
    "hk express": "UO",
    "hong kong airlines": "HX",
    "united airlines": "UA",
    "united": "UA",
    "american airlines": "AA",
    "american": "AA",
    "delta air lines": "DL",
    "delta airlines": "DL",
    "delta": "DL",
    "alaska airlines": "AS",
    "alaska": "AS",
    "southwest airlines": "WN",
    "southwest": "WN",
    "jetblue": "B6",
    "jetblue airways": "B6",
    "frontier airlines": "F9",
    "spirit airlines": "NK",
    "hawaiian airlines": "HA",
    "air canada": "AC",
}


def airline_name_to_code(name: str) -> str:
    """Best-effort IATA code for Trip.com logo URLs."""
    text = expand_airline_code((name or "").strip())
    if not text:
        return ""
    if len(text) == 2 and text.upper() in AIRLINE_CODE_MAP:
        return text.upper()
    low = text.lower()
    if low in _AIRLINE_NAME_ALIASES:
        return _AIRLINE_NAME_ALIASES[low]
    for code, full in AIRLINE_CODE_MAP.items():
        if full.lower() == low:
            return code
    if _AIRLINE_RE.search(text):
        token = _AIRLINE_RE.search(text)
        if token:
            cand = (token.group(1) or "").strip()
            if len(cand) == 2 and cand.upper() in AIRLINE_CODE_MAP:
                return cand.upper()
            alias = _AIRLINE_NAME_ALIASES.get(cand.lower())
            if alias:
                return alias
            for code, full in AIRLINE_CODE_MAP.items():
                if full.lower() == cand.lower():
                    return code
    return ""


def split_airline_names(airline: str) -> list[str]:
    """Split \"Cathay Pacific, American Airlines\" into distinct carrier names."""
    text = (airline or "").strip()
    if not text:
        return []

    found: list[str] = []
    # Prefer known airline name matches (handles joint labels)
    for m in _AIRLINE_RE.finditer(text):
        token = (m.group(1) or "").strip()
        if not token:
            continue
        full = expand_airline_code(token)
        if full and full not in found:
            found.append(full)
    if found:
        return found

    # Fallback: comma / slash / ampersand separators
    parts = re.split(r"\s*[,/&+]+\s*|\s+/\s+|\s+and\s+", text, flags=re.I)
    for part in parts:
        part = part.strip(" ·|-")
        if not part or not is_plausible_airline_name(part):
            continue
        full = expand_airline_code(part)
        if full and full not in found:
            found.append(full)
    return found or ([text] if is_plausible_airline_name(text) else [])


def airline_logo_urls(airline: str) -> list[str]:
    """Trip.com CDN logo URLs for every carrier in a multi-airline string."""
    names = split_airline_names(airline)
    if not names and (airline or "").strip():
        names = [airline.strip()]
    urls: list[str] = []
    seen: set[str] = set()
    for name in names:
        code = airline_name_to_code(name)
        if not code:
            code = airline_name_to_code(name.split(",")[0].strip())
        if not code:
            continue
        url = (
            "https://static.tripcdn.com/packages/flight/airline-logo/latest/"
            f"airline_logo/3x/{code.lower()}.png"
        )
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def airline_logo_url(airline: str) -> str:
    """Trip.com CDN URL for a carrier logo (PNG, 3x).

    For multi-carrier strings (\"A, B\"), returns the first carrier's logo.
    Prefer ``airline_logo_urls`` when painting stacked icons.
    """
    urls = airline_logo_urls(airline)
    return urls[0] if urls else ""
