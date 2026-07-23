"""Multi-city regional trip routing for vague destinations (e.g. California)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class StaySegment:
    """One hotel city stay within a regional trip."""

    city: str
    airport: str
    nights: int
    checkin: str = ""
    checkout: str = ""
    guide_key: str = ""  # destination_guides city key
    label: str = ""

    def with_dates(self, checkin: str, checkout: str) -> "StaySegment":
        return StaySegment(
            city=self.city,
            airport=self.airport,
            nights=self.nights,
            checkin=checkin,
            checkout=checkout,
            guide_key=self.guide_key or self.city.lower(),
            label=self.label or self.city,
        )


@dataclass
class RegionalRoute:
    """Open-jaw / multi-city plan for a region."""

    key: str
    label: str
    arrive_airport: str
    depart_airport: str
    stays: list[StaySegment] = field(default_factory=list)
    internal_note: str = ""


# Vague region / country-area aliases → route key
_REGION_ALIASES: dict[str, str] = {
    "california": "california",
    "ca": "california",
    "ca usa": "california",
    "cali": "california",
    "west coast usa": "california",
    "southern california": "california",
    "northern california": "california",
}


def normalize_region_key(destination: str) -> str:
    key = (destination or "").strip().lower().replace(",", " ")
    key = " ".join(key.split())
    if key in _REGION_ALIASES:
        return _REGION_ALIASES[key]
    for alias, route in _REGION_ALIASES.items():
        if alias in key:
            return route
    return ""


def is_regional_destination(destination: str) -> bool:
    return bool(normalize_region_key(destination))


def _split_nights(nights: int, parts: int) -> list[int]:
    """Split nights across cities (at least 1 each when possible)."""
    n = max(1, int(nights or 1))
    parts = max(1, parts)
    if n <= parts:
        # Short trip: put leftover nights on first cities, drop empty
        out = [1] * n
        return out
    base = n // parts
    rem = n % parts
    return [base + (1 if i < rem else 0) for i in range(parts)]


def build_regional_route(
    destination: str,
    nights: int,
    *,
    depart_date: str = "",
) -> RegionalRoute | None:
    """Build a multi-city stay plan for a vague region."""
    key = normalize_region_key(destination)
    if not key:
        return None
    n = max(1, int(nights or 1))

    if key == "california":
        # SF → LA for typical trips; add San Diego on long stays
        if n >= 8:
            night_parts = _split_nights(n, 3)
            cities = [
                ("San Francisco", "sfo", "san francisco"),
                ("Los Angeles", "lax", "los angeles"),
                ("San Diego", "san", "san diego"),
            ]
            note = "Travel SF → LA (flight ~1.5h or Amtrak Coast Starlight), then LA → San Diego (train/car ~2–3h)."
        elif n >= 4:
            night_parts = _split_nights(n, 2)
            cities = [
                ("San Francisco", "sfo", "san francisco"),
                ("Los Angeles", "lax", "los angeles"),
            ]
            note = "Mid-trip transfer SF → LA (flight ~1.5h, or Amtrak / drive ~6h)."
        else:
            # Very short: still two cities if 2–3 nights, else SF only
            if n >= 2:
                night_parts = _split_nights(n, 2)
                cities = [
                    ("San Francisco", "sfo", "san francisco"),
                    ("Los Angeles", "lax", "los angeles"),
                ]
                note = "Short California loop: SF then LA; fly SF → LA between stays."
            else:
                night_parts = [1]
                cities = [("San Francisco", "sfo", "san francisco")]
                note = "Single-night California stop in San Francisco."

        stays: list[StaySegment] = []
        cursor = depart_date
        for i, nights_i in enumerate(night_parts):
            city, airport, guide = cities[i]
            checkin = cursor
            checkout = ""
            if checkin:
                try:
                    checkout = (
                        date.fromisoformat(checkin) + timedelta(days=nights_i)
                    ).isoformat()
                    cursor = checkout
                except ValueError:
                    checkout = ""
            stays.append(
                StaySegment(
                    city=city,
                    airport=airport,
                    nights=nights_i,
                    checkin=checkin,
                    checkout=checkout,
                    guide_key=guide,
                    label=f"{city} ({nights_i} night{'s' if nights_i != 1 else ''})",
                )
            )

        return RegionalRoute(
            key=key,
            label="California",
            arrive_airport=stays[0].airport,
            depart_airport=stays[-1].airport,
            stays=stays,
            internal_note=note,
        )

    return None


def regional_day_city_sequence(route: RegionalRoute) -> list[str]:
    """One guide_key per trip day (nights in each city)."""
    seq: list[str] = []
    for stay in route.stays:
        seq.extend([stay.guide_key] * max(1, stay.nights))
    return seq
