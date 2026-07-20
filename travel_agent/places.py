"""Resolve city / airport names to Trip.com IATA / search codes."""

from __future__ import annotations

import re

# Common city/airport aliases → Trip.com city/airport codes
_CITY_TO_CODE: dict[str, str] = {
    # Hong Kong
    "hong kong": "hkg",
    "hk": "hkg",
    "hkg": "hkg",
    # Paris
    "paris": "par",
    "cdg": "cdg",
    "ory": "ory",
    "par": "par",
    # Tokyo
    "tokyo": "tyo",
    "tyo": "tyo",
    "nrt": "nrt",
    "hnd": "hnd",
    "narita": "nrt",
    "haneda": "hnd",
    # Osaka
    "osaka": "osa",
    "osa": "osa",
    "kix": "kix",
    "itm": "itm",
    # Taipei
    "taipei": "tpe",
    "tpe": "tpe",
    "tsa": "tsa",
    # Seoul
    "seoul": "sel",
    "sel": "sel",
    "icn": "icn",
    "gmp": "gmp",
    # Singapore
    "singapore": "sin",
    "sin": "sin",
    # Bangkok
    "bangkok": "bkk",
    "bkk": "bkk",
    "dmk": "dmk",
    # London
    "london": "lon",
    "lon": "lon",
    "lhr": "lhr",
    "lgw": "lgw",
    # New York
    "new york": "nyc",
    "nyc": "nyc",
    "jfk": "jfk",
    "ewr": "ewr",
    # Shanghai / Beijing / Guangzhou / Shenzhen
    "shanghai": "sha",
    "sha": "sha",
    "pvg": "pvg",
    "beijing": "bjs",
    "bjs": "bjs",
    "pek": "pek",
    "pkx": "pkx",
    "guangzhou": "can",
    "can": "can",
    "shenzhen": "szx",
    "szx": "szx",
    # Others often used from HK
    "macau": "mfm",
    "macao": "mfm",
    "mfm": "mfm",
    "manila": "mnl",
    "mnl": "mnl",
    "kuala lumpur": "kul",
    "kul": "kul",
    "jakarta": "jkt",
    "jkt": "jkt",
    "bali": "dps",
    "denpasar": "dps",
    "dps": "dps",
    "sydney": "syd",
    "syd": "syd",
    "melbourne": "mel",
    "mel": "mel",
    "los angeles": "lax",
    "lax": "lax",
    "san francisco": "sfo",
    "sfo": "sfo",
    "vancouver": "yvr",
    "yvr": "yvr",
    "toronto": "yyz",
    "yyz": "yyz",
    "rome": "rom",
    "rom": "rom",
    "fco": "fco",
    "milan": "mil",
    "mil": "mil",
    "barcelona": "bcn",
    "bcn": "bcn",
    "amsterdam": "ams",
    "ams": "ams",
    "frankfurt": "fra",
    "fra": "fra",
    "munich": "muc",
    "muc": "muc",
    "dubai": "dxb",
    "dxb": "dxb",
    "taichung": "rmq",
    "rmq": "rmq",
    "kaohsiung": "khh",
    "khh": "khh",
    "fukuoka": "fuk",
    "fuk": "fuk",
    "okinawa": "oka",
    "oka": "oka",
    "sapporo": "spk",
    "spk": "spk",
    "cts": "cts",
}


def normalize_place(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.replace(".", "")
    return text


def to_flight_code(value: str) -> str:
    """Map a city/airport name or code to a Trip.com flight city code."""
    key = normalize_place(value)
    if not key:
        return ""
    if key in _CITY_TO_CODE:
        return _CITY_TO_CODE[key]
    # Already a 3-letter code
    if re.fullmatch(r"[a-z]{3}", key):
        return key
    # Try last token (e.g. "Paris France")
    parts = key.split()
    if len(parts) > 1:
        for part in (parts[0], parts[-1]):
            if part in _CITY_TO_CODE:
                return _CITY_TO_CODE[part]
            if re.fullmatch(r"[a-z]{3}", part):
                return part
    return key.replace(" ", "")


# Trip.com hotel list requires numeric city= IDs (city names return 0 results).
_HOTEL_CITY_IDS: dict[str, tuple[str, str]] = {
    "hong kong": ("58", "Hong Kong"),
    "hk": ("58", "Hong Kong"),
    "hkg": ("58", "Hong Kong"),
    "paris": ("192", "Paris"),
    "par": ("192", "Paris"),
    "cdg": ("192", "Paris"),
    "ory": ("192", "Paris"),
    "tokyo": ("228", "Tokyo"),
    "tyo": ("228", "Tokyo"),
    "nrt": ("228", "Tokyo"),
    "hnd": ("228", "Tokyo"),
    "osaka": ("60", "Osaka"),
    "osa": ("60", "Osaka"),
    "kix": ("60", "Osaka"),
    "taipei": ("617", "Taipei"),
    "tpe": ("617", "Taipei"),
    "seoul": ("274", "Seoul"),
    "sel": ("274", "Seoul"),
    "icn": ("274", "Seoul"),
    "singapore": ("73", "Singapore"),
    "sin": ("73", "Singapore"),
    "bangkok": ("359", "Bangkok"),
    "bkk": ("359", "Bangkok"),
    "london": ("338", "London"),
    "lon": ("338", "London"),
    "lhr": ("338", "London"),
    "new york": ("633", "New York"),
    "nyc": ("633", "New York"),
    "jfk": ("633", "New York"),
    "shanghai": ("2", "Shanghai"),
    "sha": ("2", "Shanghai"),
    "pvg": ("2", "Shanghai"),
    "beijing": ("1", "Beijing"),
    "bjs": ("1", "Beijing"),
    "pek": ("1", "Beijing"),
    "guangzhou": ("32", "Guangzhou"),
    "can": ("32", "Guangzhou"),
    "shenzhen": ("30", "Shenzhen"),
    "szx": ("30", "Shenzhen"),
    "macau": ("59", "Macau"),
    "macao": ("59", "Macau"),
    "mfm": ("59", "Macau"),
    "rome": ("213", "Rome"),
    "rom": ("213", "Rome"),
    "fco": ("213", "Rome"),
    "barcelona": ("375", "Barcelona"),
    "bcn": ("375", "Barcelona"),
    "amsterdam": ("316", "Amsterdam"),
    "ams": ("316", "Amsterdam"),
    "dubai": ("220", "Dubai"),
    "dxb": ("220", "Dubai"),
    "sydney": ("158", "Sydney"),
    "syd": ("158", "Sydney"),
    "melbourne": ("152", "Melbourne"),
    "mel": ("152", "Melbourne"),
    "los angeles": ("347", "Los Angeles"),
    "lax": ("347", "Los Angeles"),
    "san francisco": ("346", "San Francisco"),
    "sfo": ("346", "San Francisco"),
    "manila": ("364", "Manila"),
    "mnl": ("364", "Manila"),
    "kuala lumpur": ("45", "Kuala Lumpur"),
    "kul": ("45", "Kuala Lumpur"),
}


def to_hotel_city(value: str) -> str:
    """Hotel searches prefer human city names."""
    key = normalize_place(value)
    if key in _HOTEL_CITY_IDS:
        return _HOTEL_CITY_IDS[key][1]
    # If user passed an airport code, expand to a city name when known
    code_to_city = {
        "hkg": "Hong Kong",
        "par": "Paris",
        "cdg": "Paris",
        "ory": "Paris",
        "tyo": "Tokyo",
        "nrt": "Tokyo",
        "hnd": "Tokyo",
        "tpe": "Taipei",
        "sel": "Seoul",
        "icn": "Seoul",
        "sin": "Singapore",
        "bkk": "Bangkok",
        "lon": "London",
        "lhr": "London",
        "nyc": "New York",
    }
    if key in code_to_city:
        return code_to_city[key]
    # Title-case multi-word cities
    if key in _CITY_TO_CODE and not re.fullmatch(r"[a-z]{3}", key):
        return value.strip().title() if value.strip() else key.title()
    return value.strip() or key


def to_hotel_city_id(value: str) -> str | None:
    """Return Trip.com numeric hotel city ID when known."""
    key = normalize_place(value)
    if key in _HOTEL_CITY_IDS:
        return _HOTEL_CITY_IDS[key][0]
    # Already a numeric id
    if re.fullmatch(r"\d{1,6}", key):
        return key
    return None
