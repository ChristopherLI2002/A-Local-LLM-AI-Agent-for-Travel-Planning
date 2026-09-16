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
    "florida": "florida",
    "fl": "florida",
    "fl usa": "florida",
    "florida usa": "florida",
    "southern florida": "florida",
}


def normalize_region_key(destination: str) -> str:
    import re

    key = (destination or "").strip().lower().replace(",", " ")
    key = " ".join(key.split())
    if not key:
        return ""
    if key in _REGION_ALIASES:
        return _REGION_ALIASES[key]
    # Prefer longer aliases first so "southern california" wins over "ca"
    for alias, route in sorted(_REGION_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if " " in alias:
            if alias in key:
                return route
            continue
        # Whole-word only for compact aliases (prevents "ca" matching "macau")
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", key):
            return route
    return ""


def is_regional_destination(destination: str) -> bool:
    return bool(normalize_region_key(destination))


# Country / continent labels that must never become hotel stay cities.
_COUNTRY_ONLY = frozenset(
    {
        "japan",
        "korea",
        "south korea",
        "north korea",
        "china",
        "france",
        "italy",
        "spain",
        "germany",
        "uk",
        "united kingdom",
        "england",
        "usa",
        "us",
        "united states",
        "america",
        "thailand",
        "vietnam",
        "indonesia",
        "malaysia",
        "philippines",
        "australia",
        "canada",
        "taiwan",
        "macau",
        "macao",
    }
)


def _is_country_only_label(name: str) -> bool:
    key = " ".join((name or "").strip().lower().replace(",", " ").split())
    return key in _COUNTRY_ONLY


def parse_stay_cities_arg(stay_cities: str, *, total_nights: int) -> list[tuple[str, int]]:
    """Parse ``San Francisco:4,Los Angeles:3`` — never ``Tokyo, Japan`` as two stays.

    Models often pass destination-shaped strings (``Tokyo, Japan``) into stay_cities.
    Those must collapse to one city. Country-only tokens are dropped.
    """
    raw = (stay_cities or "").strip()
    if not raw:
        return []

    n = max(1, int(total_nights or 1))
    # "Tokyo:4, Osaka:3" style — only split on commas that separate City:N segments
    if ":" in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        # No night markers → treat whole string as one place (strip ", Country")
        parts = [raw]

    parsed: list[tuple[str, int]] = []
    for part in parts:
        if ":" in part:
            city_s, nights_s = part.rsplit(":", 1)
            city_s = city_s.strip()
            # "Tokyo, Japan:4" → city head before country
            if "," in city_s:
                head, tail = city_s.split(",", 1)
                if _is_country_only_label(tail):
                    city_s = head.strip()
            try:
                nights_i = max(1, int(nights_s.strip()))
            except ValueError:
                nights_i = 0
            if city_s and not _is_country_only_label(city_s):
                parsed.append((city_s, nights_i))
        else:
            city_s = part.strip()
            if "," in city_s:
                head, tail = city_s.split(",", 1)
                if _is_country_only_label(tail):
                    city_s = head.strip()
                elif not _is_country_only_label(head):
                    # Ambiguous "A, B" without nights — keep first city only
                    city_s = head.strip()
            if city_s and not _is_country_only_label(city_s):
                parsed.append((city_s, 0))

    # Drop country tokens that slipped through (e.g. second half of bad splits)
    parsed = [(c, ni) for c, ni in parsed if c and not _is_country_only_label(c)]
    if not parsed:
        return []

    # Single remaining city → all nights there
    if len(parsed) == 1:
        return [(parsed[0][0], n)]

    fixed = sum(x[1] for x in parsed if x[1] > 0)
    zeros = [i for i, x in enumerate(parsed) if x[1] <= 0]
    remain = max(0, n - fixed)
    if zeros:
        split = _split_nights(remain if remain > 0 else len(zeros), len(zeros))
        for i, idx in enumerate(zeros):
            city_s, _ = parsed[idx]
            parsed[idx] = (city_s, split[i] if i < len(split) else 1)
    total = sum(x[1] for x in parsed) or 1
    if total != n and parsed:
        adj = n - (total - parsed[-1][1])
        parsed[-1] = (parsed[-1][0], max(1, adj))
    return parsed


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


def align_route_to_nights(
    route: RegionalRoute,
    nights: int,
    *,
    depart_date: str = "",
) -> RegionalRoute:
    """Redistribute stay nights so they sum to ``nights`` and chain check-in dates.

    Keeps the same cities/airports; only night counts and dates change. Used when
    a rough propose_trip_route undershoots the real trip length (e.g. 8 nights
    proposed for a 14-night trip).
    """
    n = max(1, int(nights or 1))
    if not route.stays:
        return route

    old = [max(1, int(s.nights or 1)) for s in route.stays]
    old_sum = sum(old) or len(old)
    if old_sum == n:
        parts = old
    elif len(route.stays) == 1:
        parts = [n]
    else:
        # Proportional to the proposed split, then fix rounding so sum == n
        parts = []
        allocated = 0
        for i, w in enumerate(old):
            remaining_cities = len(old) - i - 1
            if remaining_cities == 0:
                parts.append(max(1, n - allocated))
            else:
                ni = max(1, int(round(n * w / old_sum)))
                ni = min(ni, max(1, n - allocated - remaining_cities))
                parts.append(ni)
                allocated += ni
        drift = n - sum(parts)
        if drift != 0:
            parts[-1] = max(1, parts[-1] + drift)

    cursor = (depart_date or "").strip() or (route.stays[0].checkin or "")
    new_stays: list[StaySegment] = []
    for stay, nights_i in zip(route.stays, parts):
        checkin = cursor
        checkout = ""
        if checkin:
            try:
                checkout = (
                    date.fromisoformat(checkin) + timedelta(days=nights_i)
                ).isoformat()
                cursor = checkout
            except ValueError:
                checkout = stay.checkout or ""
        new_stays.append(
            StaySegment(
                city=stay.city,
                airport=stay.airport,
                nights=nights_i,
                checkin=checkin or stay.checkin,
                checkout=checkout or stay.checkout,
                guide_key=stay.guide_key or stay.city.lower(),
                label=f"{stay.city} ({nights_i} night{'s' if nights_i != 1 else ''})",
            )
        )
    route.stays = new_stays
    return route


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

    if key == "florida":
        # Miami → Orlando for typical trips; open-jaw MIA in / MCO out
        if n >= 4:
            night_parts = _split_nights(n, 2)
            cities = [
                ("Miami", "mia", "miami"),
                ("Orlando", "mco", "orlando"),
            ]
            note = "Mid-trip transfer Miami → Orlando (drive ~3.5h or short hop)."
        elif n >= 2:
            night_parts = _split_nights(n, 2)
            cities = [
                ("Miami", "mia", "miami"),
                ("Orlando", "mco", "orlando"),
            ]
            note = "Short Florida loop: Miami then Orlando."
        else:
            night_parts = [1]
            cities = [("Miami", "mia", "miami")]
            note = "Single-night Florida stop in Miami."

        stays = []
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
            label="Florida",
            arrive_airport=stays[0].airport,
            depart_airport=stays[-1].airport,
            stays=stays,
            internal_note=note,
        )

    return None


def build_single_city_route(
    destination: str,
    nights: int,
    *,
    depart_date: str = "",
) -> RegionalRoute:
    """One-city stay: fly into and out of the same hub."""
    from travel_agent.places import to_flight_code, to_hotel_city

    n = max(1, int(nights or 1))
    city = to_hotel_city(destination) or (destination or "Destination").strip()
    if "," in city:
        city = city.split(",", 1)[0].strip() or city
    airport = (to_flight_code(destination) or to_flight_code(city) or "").lower()
    if airport == "xxx":
        airport = ""
    checkin = depart_date
    checkout = ""
    if checkin:
        try:
            checkout = (
                date.fromisoformat(checkin) + timedelta(days=n)
            ).isoformat()
        except ValueError:
            checkout = ""
    stay = StaySegment(
        city=city,
        airport=airport,
        nights=n,
        checkin=checkin,
        checkout=checkout,
        guide_key=city.lower(),
        label=f"{city} ({n} night{'s' if n != 1 else ''})",
    )
    return RegionalRoute(
        key="single",
        label=city,
        arrive_airport=airport,
        depart_airport=airport,
        stays=[stay],
        internal_note=f"Single-city stay in {city}; round-trip via {airport.upper()}.",
    )


def propose_trip_route(
    destination: str,
    nights: int,
    *,
    depart_date: str = "",
    arrive_city: str = "",
    return_city: str = "",
    stay_cities: str = "",
    interests: str = "",
) -> RegionalRoute:
    """Decide arrive/return hubs and stay sequence before scraping flights/hotels.

    Optional overrides:
      arrive_city / return_city — force open-jaw hubs
      stay_cities — \"San Francisco:4,Los Angeles:3\" night splits
    """
    from travel_agent.places import to_flight_code, to_hotel_city

    n = max(1, int(nights or 1))
    # Explicit stay list from the agent
    parsed_stays = parse_stay_cities_arg(stay_cities, total_nights=n)
    if parsed_stays:
        stays: list[StaySegment] = []
        cursor = depart_date
        for city_s, nights_i in parsed_stays:
            city = to_hotel_city(city_s) or city_s
            if "," in city:
                city = city.split(",", 1)[0].strip() or city
            airport = (
                to_flight_code(city_s) or to_flight_code(city) or ""
            ).lower()
            if airport == "xxx":
                airport = ""
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
                    guide_key=city.lower(),
                    label=f"{city} ({nights_i} night{'s' if nights_i != 1 else ''})",
                )
            )
        arrive = (
            to_flight_code(arrive_city)
            or (stays[0].airport if stays else "")
        )
        depart = (
            to_flight_code(return_city)
            or (stays[-1].airport if stays else arrive)
        )
        cities_note = " → ".join(s.city for s in stays)
        return RegionalRoute(
            key="custom",
            label=destination.strip() or "Custom route",
            arrive_airport=(arrive or "").lower(),
            depart_airport=(depart or arrive or "").lower(),
            stays=stays,
            internal_note=(
                f"Custom route: {cities_note}. "
                f"Fly in {arrive.upper() if arrive else '?'}, "
                f"fly out {depart.upper() if depart else '?'}. "
                f"Styles: {interests or 'First-time'}."
            ),
        )

    # Known multi-city regions
    regional = build_regional_route(destination, n, depart_date=depart_date)
    if regional:
        if arrive_city:
            regional.arrive_airport = to_flight_code(arrive_city) or regional.arrive_airport
        if return_city:
            regional.depart_airport = to_flight_code(return_city) or regional.depart_airport
        if interests:
            regional.internal_note = (
                f"{regional.internal_note} Travel style focus: {interests}."
            ).strip()
        return regional

    # Single-city default (optionally different return city → open-jaw)
    base = build_single_city_route(destination, n, depart_date=depart_date)
    if arrive_city:
        base.arrive_airport = to_flight_code(arrive_city) or base.arrive_airport
        if base.stays:
            base.stays[0] = StaySegment(
                city=to_hotel_city(arrive_city) or base.stays[0].city,
                airport=base.arrive_airport,
                nights=base.stays[0].nights,
                checkin=base.stays[0].checkin,
                checkout=base.stays[0].checkout,
                guide_key=(to_hotel_city(arrive_city) or base.stays[0].city).lower(),
                label=base.stays[0].label,
            )
    if return_city:
        base.depart_airport = to_flight_code(return_city) or base.depart_airport
        if (
            base.depart_airport.upper() != base.arrive_airport.upper()
            and len(base.stays) == 1
            and n >= 2
        ):
            # Split into arrive city + return city stays
            half = _split_nights(n, 2)
            c1 = to_hotel_city(arrive_city or destination) or base.stays[0].city
            c2 = to_hotel_city(return_city) or return_city
            a1 = base.arrive_airport
            a2 = base.depart_airport
            cursor = depart_date
            new_stays: list[StaySegment] = []
            for city, airport, nights_i in (
                (c1, a1, half[0]),
                (c2, a2, half[1]),
            ):
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
                new_stays.append(
                    StaySegment(
                        city=city,
                        airport=airport,
                        nights=nights_i,
                        checkin=checkin,
                        checkout=checkout,
                        guide_key=city.lower(),
                        label=f"{city} ({nights_i} night{'s' if nights_i != 1 else ''})",
                    )
                )
            base.stays = new_stays
            base.key = "openjaw"
            base.internal_note = (
                f"Open-jaw single-region idea: {c1} then {c2}; "
                f"{a1.upper()} in / {a2.upper()} out. Styles: {interests or 'First-time'}."
            )
        elif interests:
            base.internal_note = (
                f"{base.internal_note} Travel style focus: {interests}."
            ).strip()
    elif interests:
        base.internal_note = (
            f"{base.internal_note} Travel style focus: {interests}."
        ).strip()
    return base


def format_route_proposal(route: RegionalRoute, *, origin: str = "Hong Kong") -> str:
    """Human + agent-readable rough plan before flight/hotel search."""
    from travel_agent.places import to_flight_code

    origin_code = (to_flight_code(origin) or "hkg").upper()
    arrive = (route.arrive_airport or "").upper()
    depart = (route.depart_airport or "").upper()
    open_jaw = bool(arrive and depart and arrive != depart)
    lines = [
        "ROUGH TRIP ROUTE (decide before searching flights/hotels)",
        f"- Destination label: {route.label}",
        f"- Fly into (arrive airport): {arrive}",
        f"- Fly out (return airport): {depart}",
        f"- Open-jaw: {'yes' if open_jaw else 'no (round-trip)'}",
        f"- Origin: {origin} ({origin_code})",
        "- Stays:",
    ]
    for i, stay in enumerate(route.stays, 1):
        dates = ""
        if stay.checkin and stay.checkout:
            dates = f" · {stay.checkin} → {stay.checkout}"
        lines.append(
            f"  {i}. {stay.city} — {stay.nights} night"
            f"{'s' if stay.nights != 1 else ''} · hub {(stay.airport or '').upper()}"
            f"{dates}"
        )
    route_bits = [f"{origin_code}->{arrive}"]
    for i in range(len(route.stays) - 1):
        a = route.stays[i].city
        b = route.stays[i + 1].city
        route_bits.append(f"{a}->{b}")
    route_bits.append(f"{depart}->{origin_code}")
    lines.append(f"- Rough route: {' · '.join(route_bits)}")
    if route.internal_note:
        lines.append(f"- Notes: {route.internal_note}")
    lines.append(
        "- Next: call plan_trip with the same dates, "
        f"arrive_airport={arrive.lower()}, return_airport={depart.lower()}, "
        f"hotel_city={route.stays[0].city if route.stays else route.label}."
    )
    return "\n".join(lines)


def regional_day_city_sequence(route: RegionalRoute) -> list[str]:
    """One guide_key per trip day (nights in each city)."""
    seq: list[str] = []
    for stay in route.stays:
        seq.extend([stay.guide_key] * max(1, stay.nights))
    return seq
