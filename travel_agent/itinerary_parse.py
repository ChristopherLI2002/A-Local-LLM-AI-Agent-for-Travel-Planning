"""Parse Trip.Planner-style agent text into flight, hotel, and day sections."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from travel_agent.trip_urls import pick_booking_url
from travel_agent.airline_names import _AIRLINE_RE, is_plausible_airline_name


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_DAY_RE = re.compile(r"(?im)^\s*day\s+(\d+)\s*[:.\-]?\s*(.*)$")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_DURATION_RE = re.compile(
    r"\b(\d+\s*h(?:ours?)?(?:\s*\d+\s*m(?:ins?)?)?|\d+\s*h\s*\d+\s*m|\d+h\s*\d+m)\b",
    re.I,
)
_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b(?:\s*(T\d+))?")
_PRICE_RE = re.compile(
    r"(?:HK\s*\$|HKD\s*)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+)",
    re.I,
)


@dataclass
class Block:
    title: str
    body: str
    urls: list[str] = field(default_factory=list)


@dataclass
class FlightOffer:
    """Structured fields for a Trip.com-style flight result row."""

    airline: str = ""
    depart_time: str = ""
    depart_airport: str = ""
    arrive_time: str = ""
    arrive_airport: str = ""
    duration: str = ""
    stops: str = "Direct"
    price_label: str = ""
    trip_label: str = "Return"
    badge: str = ""
    baggage: str = ""
    airline_logo: str = ""
    return_airline: str = ""
    return_depart_time: str = ""
    return_depart_airport: str = ""
    return_arrive_time: str = ""
    return_arrive_airport: str = ""
    return_duration: str = ""
    return_stops: str = ""
    return_airline_logo: str = ""
    url: str = ""
    raw: str = ""


@dataclass
class HotelOffer:
    """Structured fields for a Trip.com-style hotel listing card."""

    name: str = ""
    stars: int = 0
    score: str = ""
    score_label: str = ""
    reviews: str = ""
    location: str = ""
    features: str = ""
    room_type: str = ""
    beds: str = ""
    social_proof: str = ""
    price_label: str = ""
    total_label: str = ""
    url: str = ""
    image_url: str = ""
    raw: str = ""


@dataclass
class ParsedItinerary:
    flight: Block | None = None
    hotel: Block | None = None
    days: list[Block] = field(default_factory=list)
    budget: str = ""
    raw: str = ""
    flight_offer: FlightOffer | None = None
    hotel_offer: HotelOffer | None = None


def _urls_in(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _slice_section(text: str, start_pat: str, end_pats: list[str]) -> str:
    start = re.search(start_pat, text, re.I | re.M)
    if not start:
        return ""
    rest = text[start.end() :]
    end_pos = len(rest)
    for pat in end_pats:
        m = re.search(pat, rest, re.I | re.M)
        if m and m.start() < end_pos:
            end_pos = m.start()
    return rest[:end_pos].strip()


def parse_flight_offer(text: str, fallback_url: str = "") -> FlightOffer:
    """Best-effort parse of a recommended-flight block into row fields."""
    blob = text or ""
    offer = FlightOffer(raw=blob, url=fallback_url)

    urls = [u for u in _urls_in(blob) if "trip.com" in u.lower()]
    offer.url = pick_booking_url(urls, "flight", fallback=fallback_url)

    airline_m = _AIRLINE_RE.search(blob)
    if airline_m:
        offer.airline = airline_m.group(1)
        code_map = {
            "CX": "Cathay Pacific",
            "HB": "Greater Bay Airlines",
            "UO": "HK Express",
            "AF": "Air France",
            "KL": "KLM",
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
        offer.airline = code_map.get(offer.airline.upper(), offer.airline)

    times = _TIME_RE.findall(blob)
    if len(times) >= 2:
        offer.depart_time = f"{int(times[0][0]):02d}:{times[0][1]}"
        offer.arrive_time = f"{int(times[1][0]):02d}:{times[1][1]}"
    elif len(times) == 1:
        offer.depart_time = f"{int(times[0][0]):02d}:{times[0][1]}"

    for key, attr in (
        (r"depart(?:ure)?\s*(?:time)?\s*[:\-]\s*([0-2]?\d:[0-5]\d)", "depart_time"),
        (r"arriv(?:e|al)\s*(?:time)?\s*[:\-]\s*([0-2]?\d:[0-5]\d)", "arrive_time"),
        (r"duration\s*[:\-]\s*([^\n,;]+)", "duration"),
        (r"airline\s*[:\-]\s*([^\n,;]+)", "airline"),
        (r"from\s*[:\-]\s*([A-Z]{3}(?:\s*T\d+)?)", "depart_airport"),
        (r"to\s*[:\-]\s*([A-Z]{3}(?:\s*T\d+)?)", "arrive_airport"),
    ):
        m = re.search(key, blob, re.I)
        if m and not getattr(offer, attr):
            setattr(offer, attr, m.group(1).strip())

    dur_m = _DURATION_RE.search(blob)
    if dur_m and not offer.duration:
        cand = re.sub(r"\s+", " ", dur_m.group(1)).strip()
        # Reject hotel-stay phrasing that sometimes leaks into flight blocks
        if "night" not in cand.lower() and "july" not in cand.lower():
            offer.duration = cand

    # Labeled duration can also be polluted — re-check
    if offer.duration and (
        "night" in offer.duration.lower() or re.search(r"(?i)july|aug|sep|oct|nov|dec", offer.duration)
    ):
        offer.duration = ""

    route_m = re.search(
        r"\b([A-Z]{3})(?:\s*(T\d+))?\s*(?:→|->|to)\s*([A-Z]{3})(?:\s*(T\d+))?",
        blob,
    )
    if route_m:
        offer.depart_airport = (
            f"{route_m.group(1)}" + (f" {route_m.group(2)}" if route_m.group(2) else "")
        )
        offer.arrive_airport = (
            f"{route_m.group(3)}" + (f" {route_m.group(4)}" if route_m.group(4) else "")
        )
    else:
        airports = _AIRPORT_RE.findall(blob)
        noise = {"HKD", "URL", "AND", "THE", "FOR", "DAY", "EST", "LOW", "SEE"}
        clean = [(c, t) for c, t in airports if c not in noise]
        if len(clean) >= 2 and not offer.depart_airport:
            a0, t0 = clean[0]
            a1, t1 = clean[1]
            offer.depart_airport = f"{a0}" + (f" {t0}" if t0 else "")
            offer.arrive_airport = f"{a1}" + (f" {t1}" if t1 else "")

    price_m = _PRICE_RE.search(blob)
    if price_m:
        offer.price_label = f"HK${price_m.group(1)}"

    low = blob.lower()
    if "direct" in low or "non-stop" in low or "nonstop" in low:
        offer.stops = "Direct"
        offer.badge = "Cheapest direct" if ("cheap" in low or offer.price_label) else "Direct"
    elif "stop" in low:
        stop_m = re.search(r"(\d+)\s*stop", low)
        offer.stops = f"{stop_m.group(1)} stop" if stop_m else "1 stop"
        offer.badge = "Best value" if offer.price_label else "Recommended"
    else:
        offer.badge = "Recommended"

    bag_m = re.search(r"(?i)(?:checked\s*)?baggage[^\n]{0,40}?(\d+\s*kg)", blob)
    if bag_m:
        offer.baggage = f"Checked baggage {bag_m.group(1)}"
    elif "baggage" in low or "checked bag" in low:
        offer.baggage = "Checked baggage 20 kg"

    if re.search(r"(?i)\bone[- ]?way\b", blob):
        offer.trip_label = "One-way"
    else:
        offer.trip_label = "Return"

    if not is_plausible_airline_name(offer.airline):
        offer.airline = ""

    if not offer.airline:
        offer.airline = "Trip.com fare"
    if not offer.depart_time:
        offer.depart_time = "--:--"
    if not offer.arrive_time:
        offer.arrive_time = "--:--"
    if not offer.duration:
        offer.duration = "—"
    if not offer.depart_airport:
        offer.depart_airport = "HKG"
    if not offer.arrive_airport:
        offer.arrive_airport = "—"
    if not offer.price_label:
        offer.price_label = "See Trip.com"
    if not offer.badge:
        offer.badge = "Recommended"

    return offer


def _labeled(blob: str, *keys: str) -> str:
    for key in keys:
        m = re.search(rf"(?im)^[\-\*\u2022]?\s*{key}\s*[:\-]\s*(.+)$", blob)
        if m:
            return m.group(1).strip()
    return ""


def parse_hotel_offer(text: str, fallback_url: str = "") -> HotelOffer:
    """Best-effort parse of a recommended-hotel block into listing-card fields."""
    blob = text or ""
    offer = HotelOffer(raw=blob, url=fallback_url)

    urls = [u for u in _urls_in(blob) if "trip.com" in u.lower()]
    offer.url = pick_booking_url(urls, "hotel", fallback=fallback_url)

    img = re.search(
        r"(?i)(?:image|photo|cover)\s*[:\-]\s*(https?://\S+\.(?:jpg|jpeg|png|webp)\S*)",
        blob,
    )
    if img:
        offer.image_url = img.group(1).rstrip(".,;)")

    offer.name = _labeled(blob, "hotel", "name", "property")
    if offer.name and (
        offer.name.startswith("#")
        or "day-by-day" in offer.name.lower()
        or "sample hotel" in offer.name.lower()
        or "rates range" in offer.name.lower()
        or "option" in offer.name.lower() and "hotel" in offer.name.lower()
        or len(offer.name) > 70
    ):
        offer.name = ""
    if not offer.name:
        # First non-bullet line that looks like a title
        for line in blob.splitlines():
            s = line.strip(" -*\t#")
            if not s or s.lower().startswith(
                (
                    "link",
                    "price",
                    "check",
                    "room",
                    "location",
                    "feature",
                    "score",
                    "star",
                    "total",
                    "bed",
                    "nightly",
                    "hotel:",
                )
            ):
                continue
            if "http" in s.lower() or "day-by-day" in s.lower() or "itinerary" in s.lower():
                continue
            if "sample hotel" in s.lower() or "rates range" in s.lower():
                continue
            if len(s) > 70:
                continue
            if len(s) > 3:
                offer.name = s[:80]
                break

    stars_m = re.search(r"(?i)(?:stars?|star rating)\s*[:\-]?\s*([1-5])\b", blob)
    if stars_m:
        offer.stars = int(stars_m.group(1))
    else:
        star_glyphs = blob.count("★") + blob.count("⭐")
        if 1 <= star_glyphs <= 5:
            offer.stars = star_glyphs

    score_m = re.search(r"(?i)(?:score|rating)\s*[:\-]?\s*(\d(?:\.\d)?)\b", blob)
    if score_m:
        offer.score = score_m.group(1)
    else:
        loose = re.search(r"\b([89](?:\.\d)?|10(?:\.0)?)\b", blob)
        if loose:
            offer.score = loose.group(1)

    if offer.score:
        try:
            val = float(offer.score)
            if val >= 9.0:
                offer.score_label = "Great"
            elif val >= 8.0:
                offer.score_label = "Very Good"
            elif val >= 7.0:
                offer.score_label = "Good"
            else:
                offer.score_label = "Okay"
        except ValueError:
            offer.score_label = "Rated"

    rev_m = re.search(r"(?i)(\d[\d,]*)\s*reviews?", blob)
    if rev_m:
        offer.reviews = f"{rev_m.group(1)} reviews"

    offer.location = _labeled(blob, "location", "area", "neighbourhood", "neighborhood", "district")
    offer.features = _labeled(blob, "features", "highlights", "amenities")
    offer.room_type = _labeled(blob, "room", "room type")
    offer.beds = _labeled(blob, "beds", "bed", "bed type")
    offer.social_proof = _labeled(blob, "social", "social proof", "popularity")

    prices = list(_PRICE_RE.finditer(blob))
    if prices:
        offer.price_label = f"HK${prices[0].group(1)}"
        if len(prices) >= 2:
            offer.total_label = f"Total (incl. taxes & fees): HK${prices[1].group(1)}"
        else:
            total_m = re.search(
                r"(?i)total[^\n]{0,40}?HK\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)",
                blob,
            )
            if total_m:
                offer.total_label = f"Total (incl. taxes & fees): HK${total_m.group(1)}"

    nightly = _labeled(blob, "nightly", "price", "from")
    if nightly and not offer.price_label:
        pm = _PRICE_RE.search(nightly) or re.search(r"([0-9,]+)", nightly)
        if pm:
            offer.price_label = (
                f"HK${pm.group(1)}" if "HK" in nightly.upper() else f"HK${pm.group(1)}"
            )

    if not offer.name:
        offer.name = "Recommended hotel"
    if not offer.location:
        offer.location = "See map on Trip.com"
    if not offer.room_type:
        offer.room_type = "Standard room"
    if not offer.beds:
        offer.beds = "See room options"
    if not offer.features:
        offer.features = "Details on Trip.com"
    if not offer.price_label:
        offer.price_label = "See Trip.com"
    if not offer.score:
        offer.score = "—"
        offer.score_label = "Guest rating"
    if not offer.reviews:
        offer.reviews = "reviews on Trip.com"
    if not offer.social_proof:
        offer.social_proof = "Live rates from Trip.com Hong Kong"
    if offer.stars <= 0:
        offer.stars = 4

    return offer


def parse_itinerary(text: str) -> ParsedItinerary:
    """Split agent output into recommended flight/hotel + day blocks."""
    raw = text or ""
    result = ParsedItinerary(raw=raw)

    flight_ends = [
        r"^#{0,6}\s*recommended\s+hotel\b",
        r"^#{0,6}\s*day-by-day",
        r"^#{0,6}\s*day\s+\d+",
        r"^day\s+\d+",
        r"^budget\b",
        r"^overview\b",
        r"^transport\b",
    ]
    hotel_ends = [
        r"^#{0,6}\s*day-by-day",
        r"^#{0,6}\s*day\s+\d+",
        r"^day\s+\d+",
        r"^budget\b",
        r"^overview\b",
        r"^transport\b",
        r"^suggested\s+day",
    ]

    flight_body = _slice_section(
        raw,
        r"^#{0,6}\s*recommended\s+flight\b.*",
        flight_ends,
    )
    if not flight_body:
        flight_body = _slice_section(
            raw,
            r"recommended\s+flight\b",
            flight_ends + [r"=======", r"recommended\s+hotel\b"],
        )

    hotel_body = _slice_section(
        raw,
        r"^#{0,6}\s*recommended\s+hotel\b.*",
        hotel_ends,
    )
    if not hotel_body:
        hotel_body = _slice_section(
            raw,
            r"recommended\s+hotel\b",
            hotel_ends,
        )

    if flight_body:
        flight_body = re.sub(
            r"(?i)^#{0,6}\s*recommended\s+flight\s*[:.\-]?\s*", "", flight_body
        ).strip()
        urls = [u for u in _urls_in(flight_body) if "trip.com" in u.lower()]
        if not urls:
            urls = [
                u
                for u in _urls_in(raw)
                if "trip.com" in u.lower() and "flight" in u.lower()
            ]
        best = pick_booking_url(urls, "flight")
        if best:
            urls = [best] + [u for u in urls if u != best]
        result.flight = Block(title="Recommended flight", body=flight_body, urls=urls)
        result.flight_offer = parse_flight_offer(
            flight_body, fallback_url=best or (urls[0] if urls else "")
        )

    if hotel_body:
        hotel_body = re.sub(
            r"(?i)^#{0,6}\s*recommended\s+hotel\s*[:.\-]?\s*", "", hotel_body
        ).strip()
        urls = [u for u in _urls_in(hotel_body) if "trip.com" in u.lower()]
        if not urls:
            urls = [
                u
                for u in _urls_in(raw)
                if "trip.com" in u.lower() and "hotel" in u.lower()
            ]
        best = pick_booking_url(urls, "hotel")
        if best:
            urls = [best] + [u for u in urls if u != best]
        result.hotel = Block(
            title="Recommended hotel",
            body=hotel_body,
            urls=urls,
        )
        result.hotel_offer = parse_hotel_offer(
            hotel_body, fallback_url=best or (urls[0] if urls else "")
        )

    # Fallback: LLM skipped headings — still try to fill cards from whole plan
    if not result.flight_offer:
        offer = parse_flight_offer(raw)
        if (
            offer.airline != "Trip.com fare"
            or offer.depart_time != "--:--"
            or offer.price_label != "See Trip.com"
            or any("flight" in u.lower() for u in _urls_in(raw))
        ):
            result.flight_offer = offer
            result.flight = Block(title="Recommended flight", body=raw[:800], urls=_urls_in(raw))

    if not result.hotel_offer:
        offer = parse_hotel_offer(raw)
        if offer.name != "Recommended hotel" or any(
            "hotel" in u.lower() for u in _urls_in(raw)
        ):
            result.hotel_offer = offer
            result.hotel = Block(title="Recommended hotel", body=raw[:800], urls=_urls_in(raw))

    matches = list(_DAY_RE.finditer(raw))
    for i, m in enumerate(matches):
        day_num = m.group(1)
        title_extra = (m.group(2) or "").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        chunk = raw[start:end]
        budget_cut = re.search(r"(?im)^budget\b", chunk)
        if budget_cut:
            chunk = chunk[: budget_cut.start()]
        title = f"Day {day_num}" + (f" — {title_extra}" if title_extra else "")
        body = chunk.strip()
        if body or title_extra:
            result.days.append(
                Block(title=title, body=body or title_extra, urls=_urls_in(body))
            )

    budget = _slice_section(
        raw,
        r"^#{0,6}\s*budget(?:\s+snapshot|\s+breakdown|\s+vs)?\b.*",
        [r"^next\b", r"^book\b", r"^overview\b"],
    )
    if budget:
        result.budget = re.sub(r"(?i)^#{0,6}\s*budget[^\n]*\n?", "", budget).strip() or budget

    return result
