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
    "san francisco": "sfo",
    "sfo": "sfo",
    "california": "sfo",  # default flight code when region not expanded
    "ca": "sfo",
    "los angeles": "lax",
    "lax": "lax",
    "la": "lax",
    "san diego": "san",
    "san": "san",
    "miami": "mia",
    "mia": "mia",
    "orlando": "mco",
    "mco": "mco",
    "tampa": "tpa",
    "tpa": "tpa",
    "florida": "mia",  # default hub when region not expanded
    "fl": "mia",
    "fl usa": "mia",
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
    from urllib.parse import unquote_plus

    # URL-encoded names (Los+Angeles / Los%20Angeles) must become readable cities
    text = unquote_plus((value or "").strip()).lower()
    text = text.replace("+", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.replace(".", "")
    return text


def place_lookup_keys(value: str) -> list[str]:
    """Candidates for city/airport maps — strip country suffixes like 'Tokyo, Japan'."""
    key = normalize_place(value)
    if not key:
        return []
    keys: list[str] = [key]
    # "Tokyo, Japan" / "Seoul, Korea" / "Paris, France"
    if "," in key:
        head = key.split(",", 1)[0].strip()
        if head and head not in keys:
            keys.append(head)
    # "Tokyo (NRT)" / "Los Angeles (LAX)"
    bare = re.sub(r"\s*\([^)]*\)\s*", " ", key).strip()
    bare = re.sub(r"\s+", " ", bare)
    if bare and bare not in keys:
        keys.append(bare)
    return keys


def to_flight_code(value: str) -> str:
    """Map a city/airport name or code to a Trip.com flight city code (IATA)."""
    keys = place_lookup_keys(value)
    if not keys:
        return ""
    for key in keys:
        if key in _CITY_TO_CODE:
            return _CITY_TO_CODE[key]
        if re.fullmatch(r"[a-z]{3}", key):
            # Known map entry or accept unknown 3-letter Trip.com city codes
            return _CITY_TO_CODE.get(key, key)
    # Try tokens (e.g. "Miami Florida", "Paris France")
    for key in keys:
        parts = key.replace(",", " ").split()
        if len(parts) <= 1:
            continue
        for part in parts:
            if part in _CITY_TO_CODE:
                return _CITY_TO_CODE[part]
            if re.fullmatch(r"[a-z]{3}", part) and part in _CITY_TO_CODE:
                return _CITY_TO_CODE[part]
        for part in parts:
            if re.fullmatch(r"[a-z]{3}", part):
                return part
    # Never return multi-letter junk like "florida" as an airport code
    return ""

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
    "osaka": ("219", "Osaka"),
    "osa": ("219", "Osaka"),
    "kix": ("219", "Osaka"),
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
    "rome": ("343", "Rome"),
    "rom": ("343", "Rome"),
    "fco": ("343", "Rome"),
    "barcelona": ("40795", "Barcelona"),
    "bcn": ("40795", "Barcelona"),
    "amsterdam": ("176", "Amsterdam"),
    "ams": ("176", "Amsterdam"),
    "dubai": ("220", "Dubai"),
    "dxb": ("220", "Dubai"),
    "sydney": ("501", "Sydney"),
    "syd": ("501", "Sydney"),
    "melbourne": ("358", "Melbourne"),
    "mel": ("358", "Melbourne"),
    # US city IDs verified against hk.trip.com /hotels/list titles
    # (wrong IDs previously mapped SF→Lishui, Miami→Leshan, etc.)
    "los angeles": ("347", "Los Angeles"),
    "lax": ("347", "Los Angeles"),
    "la": ("347", "Los Angeles"),
    "san francisco": ("313", "San Francisco"),
    "sfo": ("313", "San Francisco"),
    "california": ("313", "San Francisco"),
    "ca": ("313", "San Francisco"),
    "san diego": ("698", "San Diego"),
    "san": ("698", "San Diego"),
    "miami": ("25773", "Miami"),
    "mia": ("25773", "Miami"),
    "florida": ("25773", "Miami"),
    "fl": ("25773", "Miami"),
    "orlando": ("1187", "Orlando"),
    "mco": ("1187", "Orlando"),
    "tampa": ("1399", "Tampa"),
    "tpa": ("1399", "Tampa"),
    "manila": ("364", "Manila"),
    "mnl": ("364", "Manila"),
    "kuala lumpur": ("315", "Kuala Lumpur"),
    "kul": ("315", "Kuala Lumpur"),
    "jakarta": ("524", "Jakarta"),
    "jkt": ("524", "Jakarta"),
    "bali": ("723", "Bali"),
    "denpasar": ("723", "Bali"),
    "dps": ("723", "Bali"),
}


def typed_place_name(value: str) -> str:
    """Human place string for Playwright typing — spaces only, never '+' / %20."""
    from urllib.parse import unquote_plus

    text = unquote_plus((value or "").strip())
    text = text.replace("+", " ")
    text = re.sub(r"%20", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def to_hotel_city(value: str) -> str:
    """Hotel searches prefer human city names (spaces, never '+')."""
    raw = typed_place_name(value)
    for key in place_lookup_keys(raw):
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
            "sfo": "San Francisco",
            "lax": "Los Angeles",
            "san": "San Diego",
            "mia": "Miami",
            "mco": "Orlando",
        }
        if key in code_to_city:
            return code_to_city[key]
        # Title-case multi-word cities
        if key in _CITY_TO_CODE and not re.fullmatch(r"[a-z]{3}", key):
            # Prefer the city head before a country comma
            head = key.split(",", 1)[0].strip()
            return head.title() if head else (raw.title() if raw else key.title())
    # Fall back to city-before-comma when the full string is unknown
    if "," in (raw or ""):
        return raw.split(",", 1)[0].strip() or raw
    return raw or normalize_place(value)


def to_hotel_city_id(value: str) -> str | None:
    """Return Trip.com numeric hotel city ID when known."""
    for key in place_lookup_keys(value):
        if key in _HOTEL_CITY_IDS:
            return _HOTEL_CITY_IDS[key][0]
        # Already a numeric id
        if re.fullmatch(r"\d{1,6}", key):
            return key
    return None