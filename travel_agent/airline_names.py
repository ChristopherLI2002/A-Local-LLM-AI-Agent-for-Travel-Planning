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
    r"United Airlines|American Airlines|Qantas|Vietnam Airlines|"
    r"Philippine Airlines|Malaysia Airlines|Cebu Pacific|Etihad Airways|SWISS|"
    r"KLM Royal Dutch Airlines|"
    r"CX|HB|UO|AF|KL|SQ|NH|JL|CI|BR|MU|CZ|CA|EK|QR|TK|AY|TG|KE|OZ"
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
}


def expand_airline_code(name: str) -> str:
    """Map 2-letter logo alt codes to full airline names."""
    text = (name or "").strip()
    if not text:
        return ""
    if len(text) == 2 and text.upper() in AIRLINE_CODE_MAP:
        return AIRLINE_CODE_MAP[text.upper()]
    return text
