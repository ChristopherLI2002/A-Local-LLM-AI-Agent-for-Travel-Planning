"""Playwright browser tools for searching Trip.com Hong Kong."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from travel_agent.config import settings
from travel_agent.airline_names import expand_airline_code, airline_logo_url, is_plausible_airline_name
from travel_agent.places import to_flight_code, to_hotel_city, to_hotel_city_id
from travel_agent.pricing import (
    format_comparison_table,
    nearby_dates,
    nights_between,
    parse_prices,
    pick_cheapest_from_comparison,
    summarize_prices,
)
from travel_agent.trip_urls import ensure_locale_curr, normalize_trip_url, extract_booking_urls

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
    return out


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
    )
    lines: list[str] = []
    for i, idea in enumerate(ideas, start=1):
        lines.append(format_day_title(idea, i) + ":")
        for bullet in format_day_body(idea).splitlines():
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


def _pick_flight_row(
    rows: list[dict[str, str]], *, lowest: float | None = None
) -> dict[str, str] | None:
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

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=settings.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
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
        self.open_home()

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
        """Navigate with retries when Trip.com interrupts with a competing redirect."""
        page = self._require_page()
        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                page.goto(url, wait_until=wait_until, timeout=timeout)
                return
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                if "interrupted" not in msg and "navigation" not in msg:
                    raise
                # Let the interrupting navigation settle, then retry the target URL
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(800 + attempt * 400)
        if last_err:
            raise last_err

    def open_home(self) -> str:
        page = self._require_page()
        self._safe_goto(TRIP_HOME)
        page.wait_for_timeout(1500)
        return f"Opened Trip.com Hong Kong home: {page.url}"

    def scrape_attractions(self, city: str, *, limit: int = 18) -> list[str]:
        """Search Trip.com things-to-do and return named attractions for a city."""
        page = self._require_page()
        city_name = to_hotel_city(city) or (city or "").strip()
        if not city_name:
            return []

        from travel_agent.places import to_hotel_city_id

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
            (
                f"{settings.trip_base_url}/things-to-do/?locale={settings.trip_locale}"
                f"&curr={settings.trip_currency}"
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

        names: list[str] = []
        seen: set[str] = set()
        skip_re = re.compile(
            r"(?i)\b(eSIM|SIM|wifi|wi-fi|JR Pass|airport express|lounge|voucher|"
            r"private car|charter|transfer bus|gift card|insurance)\b"
        )

        def _add(raw: str) -> None:
            text = re.sub(r"\s+", " ", (raw or "").strip())
            text = re.sub(r"^(No\.\s*\d+\s+of\s+.+?:\s*)", "", text, flags=re.I)
            if not text or len(text) < 3 or len(text) > 90:
                return
            if skip_re.search(text):
                return
            # Skip pure prices / ratings
            if re.fullmatch(r"[\d.,\sHK$%]+", text):
                return
            key = text.lower()
            if key in seen:
                return
            # Prefer landmark-like titles over long tour packages
            if text.count("·") > 2 or text.count("|") > 2:
                return
            seen.add(key)
            names.append(text)

        for url in candidates:
            try:
                self._safe_goto(url)
                page.wait_for_timeout(2200)
                # Try typing into search if we're on the hub page
                if "keyword=" not in url and "districtId=" not in url:
                    for sel in (
                        "input[placeholder*='Search' i]",
                        "input[type='search']",
                        "input[placeholder*='places' i]",
                    ):
                        try:
                            box = page.locator(sel).first
                            if box.count():
                                box.click(timeout=2000)
                                box.fill(city_name, timeout=2000)
                                box.press("Enter")
                                page.wait_for_timeout(2500)
                                break
                        except Exception:
                            continue

                # Collect from attraction/detail anchors and headings
                selectors = [
                    "a[href*='/travel-guide/attraction/']",
                    "a[href*='/things-to-do/detail']",
                    "a[href*='/things-to-do/']",
                    "h2",
                    "h3",
                ]
                for sel in selectors:
                    try:
                        locs = page.locator(sel)
                        count = min(locs.count(), 40)
                        for i in range(count):
                            try:
                                label = (locs.nth(i).inner_text(timeout=800) or "").strip()
                            except Exception:
                                continue
                            # Take first line only
                            label = label.split("\n")[0].strip()
                            _add(label)
                            if len(names) >= limit:
                                break
                    except Exception:
                        continue
                    if len(names) >= limit:
                        break
                if len(names) >= max(6, limit // 2):
                    break
            except Exception:
                continue

        self.last_attractions = names[:limit]
        return self.last_attractions

    def search_attractions(self, city: str, limit: int = 18) -> str:
        """Tool wrapper: list Trip.com things-to-do attractions for a city."""
        names = self.scrape_attractions(city, limit=max(6, int(limit or 18)))
        city_name = to_hotel_city(city) or city
        if not names:
            return (
                f"No attractions parsed for {city_name} on Trip.com things-to-do. "
                f"Try https://hk.trip.com/things-to-do/?locale=en-HK&curr=HKD"
            )
        lines = [
            f"TRIP.COM ATTRACTIONS — {city_name}",
            f"Source: https://hk.trip.com/things-to-do/?locale=en-HK&curr=HKD",
            "",
        ]
        for i, name in enumerate(names, 1):
            lines.append(f"{i}. {name}")
        lines.append("")
        lines.append(
            "Use these exact attraction names in the Day-by-day itinerary "
            "(pair with nearby restaurants)."
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
        """Search flights on Trip.com HK and return visible result text."""
        page = self._require_page()
        origin_raw = origin.strip()
        dest_raw = destination.strip()
        origin = to_flight_code(origin_raw)
        destination = to_flight_code(dest_raw)
        depart_date = depart_date or _default_depart()
        trip_type_norm = trip_type.strip().lower()
        is_round = trip_type_norm in {"round", "roundtrip", "rt", "2"}
        adults_n = max(1, min(int(adults or 1), 9))

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
        page.wait_for_timeout(3000)
        self._dismiss_popups()
        # Fares hydrate after calendar/API calls — wait until listing rows appear
        for _ in range(24):
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
        page.wait_for_timeout(2000)
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
        card = self._scrape_top_flight_card(
            origin=origin.upper(),
            destination=destination.upper(),
            lowest=prices[0] if prices else None,
        )
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
        """Search hotels on Trip.com HK for a city name or code."""
        page = self._require_page()
        city_name = to_hotel_city(city)
        city_id = to_hotel_city_id(city) or to_hotel_city_id(city_name)
        checkin = checkin or _default_depart(14)
        try:
            checkout = checkout or (
                date.fromisoformat(checkin) + timedelta(days=7)
            ).isoformat()
        except ValueError:
            checkout = checkout or _default_return(17)
        adults_n = max(1, min(int(adults or 2), 8))
        rooms_n = max(1, min(int(rooms or 1), 8))

        # Trip.com hotel list requires numeric city= IDs; city names → 0 results.
        params: dict[str, Any] = {
            "checkin": checkin,
            "checkout": checkout,
            "adult": adults_n,
            "crn": rooms_n,
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

        canonical = f"{settings.trip_base_url}/hotels/list?{urlencode(params)}"
        self._safe_goto(canonical)
        page.wait_for_timeout(2500)
        self._dismiss_popups()

        try:
            body_probe = page.inner_text("body")
        except Exception:
            body_probe = ""
        bad_list = (
            "No matching" in body_probe
            or "0 properties" in body_probe
            or len([p for p in parse_prices(body_probe) if p >= 200]) < 1
        )
        if bad_list or not city_id:
            hub = (
                f"{settings.trip_base_url}/hotels/"
                f"?locale={settings.trip_locale}&curr={settings.trip_currency}"
            )
            self._safe_goto(hub)
            page.wait_for_timeout(1500)
            self._dismiss_popups()
            self._try_fill_hotel_form(city_name, checkin, checkout, adults_n, rooms_n)

        self._wait_for_results(
            keywords=["HK$", "HKD", "hotel", "guest", "star", "review", "night"],
            attempts=14,
        )
        for _ in range(10):
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
        final_url = page.url if "trip.com" in page.url else canonical
        # Prefer a live list URL that has a numeric city id
        if re.search(r"[?&]city=\d+", final_url):
            canonical = ensure_locale_curr(normalize_trip_url(final_url))
        else:
            # Rebuild canonical if form fill resolved a city id into the address bar
            m = re.search(r"[?&]city=(\d+)", final_url)
            if m:
                params["city"] = m.group(1)
                params["cityName"] = city_name
                params.pop("keyword", None)
                canonical = f"{settings.trip_base_url}/hotels/list?{urlencode(params)}"
            else:
                canonical = ensure_locale_curr(canonical)

        detail_links = []
        hotel_names: list[str] = []
        for u, label in self._extract_hotel_detail_options(limit=8):
            nu = normalize_trip_url(u)
            low = nu.lower()
            if "/hotels/" not in low or "all-cities" in low:
                continue
            if "hotelid=" in low or re.search(r"hotel-detail-\d+", low) or re.search(
                r"/hotels/[^/?]+-\d+", low
            ):
                detail_links.append(ensure_locale_curr(nu))
                if label and label not in hotel_names:
                    hotel_names.append(label)
            if len(detail_links) >= 3:
                break
        details = "\n".join(f"Hotel option link: {u}" for u in detail_links)
        rec_detail = detail_links[0] if detail_links else ""
        rec_name = hotel_names[0] if hotel_names else ""
        hotel_card = self._scrape_top_hotel_card(
            city=city_name,
            fallback_name=rec_name,
            lowest=prices[0] if prices else None,
        )
        if hotel_card.get("name") and not rec_name:
            rec_name = hotel_card["name"]
        card_block = ""
        if hotel_card:
            # Prefer list-card photo; only open detail when missing (detail often
            # redirects to /hotels/booknew and races later flight navigations)
            if rec_detail and not hotel_card.get("image_url"):
                img = self.scrape_hotel_image_url(rec_detail)
                if img:
                    hotel_card["image_url"] = img
            card_block = (
                "Structured hotel card:\n"
                f"- Hotel: {hotel_card.get('name', rec_name)}\n"
                f"- Stars: {hotel_card.get('stars', '')}\n"
                f"- Score: {hotel_card.get('score', '')}\n"
                f"- Location: {hotel_card.get('location', city_name)}\n"
                f"- Nightly: {hotel_card.get('price_label', '')}\n"
                f"- Reviews: {hotel_card.get('reviews', '')}\n"
                + (
                    f"- Image: {hotel_card['image_url']}\n"
                    if hotel_card.get("image_url")
                    else ""
                )
            )
        content = body if prices and len(body) > 400 else snippet
        return _clean_text(
            f"Hotel search URL: {ensure_locale_curr(normalize_trip_url(final_url))}\n"
            f"Canonical search URL: {canonical}\n"
            + (f"Recommended hotel detail link: {rec_detail}\n" if rec_detail else "")
            + (f"Recommended hotel name: {rec_name}\n" if rec_name else "")
            + (f"{card_block}" if card_block else "")
            + f"City/keyword: {city_name}"
            + (f" (city id {params.get('city')})" if str(params.get("city", "")).isdigit() else "")
            + f"\nCheck-in: {checkin} | Check-out: {checkout}\n"
            f"Adults: {adults_n} | Rooms: {rooms_n}\n"
            f"{price_note}\n"
            + (f"{details}\n" if details else "")
            + f"\n{content}"
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
        """Search car rentals on Trip.com Hong Kong car hire."""
        page = self._require_page()
        pickup_date = pickup_date or _default_depart(21)
        dropoff_date = dropoff_date or _default_return(28)
        params = {
            "locale": settings.trip_locale,
            "curr": settings.trip_currency,
            "channelid": "14409",
        }
        # Hub + location keyword; Trip.com may rewrite to a city results URL.
        url = f"{settings.trip_base_url}/carhire/?{urlencode(params)}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        self._dismiss_popups()

        filled = self._try_fill_car_form(location, pickup_date, dropoff_date)
        if not filled:
            # Fallback: reopen hub with location in query when form fill fails.
            params["keyword"] = location
            params["pickupdate"] = pickup_date
            params["dropoffdate"] = dropoff_date
            page.goto(
                f"{settings.trip_base_url}/carhire/?{urlencode(params)}",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(2500)
            self._dismiss_popups()

        self._wait_for_results(
            keywords=["HK$", "car", "rental", "pickup", "pick-up", "day", "supplier"]
        )
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(1500)

        snippet = self._extract_list_content(
            selectors=[
                "[class*='car']",
                "[class*='Car']",
                "[class*='hire']",
                "[class*='list']",
                "main",
                "body",
            ]
        )
        return _clean_text(
            f"Car rental search URL: {page.url}\n"
            f"Pick-up location: {location}\n"
            f"Pick-up: {pickup_date} | Drop-off: {dropoff_date}\n"
            f"Form filled: {filled}\n\n{snippet}"
        )

    def _try_fill_car_form(
        self,
        location: str,
        pickup_date: str,
        dropoff_date: str,
    ) -> bool:
        """Best-effort fill of the Trip.com car hire search form."""
        page = self._require_page()
        try:
            location_box = page.locator(
                "input[placeholder*='Pick'], "
                "input[placeholder*='pick'], "
                "input[placeholder*='Location'], "
                "input[placeholder*='City'], "
                "input[aria-label*='Pick'], "
                "input[aria-label*='location']"
            ).first
            if location_box.count() == 0:
                return False
            location_box.click(timeout=3000)
            location_box.fill(location)
            page.wait_for_timeout(800)
            suggestion = page.get_by_text(location, exact=False).first
            try:
                suggestion.click(timeout=2500)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(600)

            # Dates are often prefilled; try a search/submit button.
            for label in ("Search", "Find cars", "Show cars", "Search cars"):
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if btn.count():
                    btn.first.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    return True
            # Fallback: press Enter in the location field
            location_box.press("Enter")
            page.wait_for_timeout(2500)
            return True
        except Exception:
            return False

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
    ) -> str:
        """Build a trip plan comparing transport modes + hotels on Trip.com."""
        depart_date = depart_date or _default_depart(21)
        return_date = return_date or _default_return_from(depart_date, 7)
        hotel_city = to_hotel_city(hotel_city or destination)
        nights = max(1, nights_between(depart_date, return_date))
        need_car = _wants_rental_car(rent_car, interests)
        want_flights = _as_bool(include_flights, default=True)
        want_trains = _as_bool(include_trains, default=True)
        want_transfers = _as_bool(include_transfers, default=True)
        if not want_flights and not want_trains:
            want_flights = True

        # Pull live attraction names from Trip.com things-to-do for the day plan
        attractions: list[str] = []
        try:
            attractions = self.scrape_attractions(hotel_city or destination, limit=18)
        except Exception:
            attractions = list(self.last_attractions or [])

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
            date_options = nearby_dates(depart_date, (-3, 0, 3))
            flight_compare_text = self.compare_flight_prices(
                origin=origin,
                destination=destination,
                dates=",".join(date_options),
                trip_type="roundtrip",
                return_date=return_date,
                adults=adults,
            )
            best_flight = pick_cheapest_from_comparison(flight_compare_text)
            recommended_flight_date = best_flight.get("depart_date") or depart_date
            recommended_flight_price = best_flight.get("lowest_hkd")
            recommended_flight_url = best_flight.get("url") or ""
            flight_best_label = best_flight.get("label") or ""

            # Open the recommended (cheapest) date so the booking link is exact
            flight_text = self.search_flights(
                origin=origin,
                destination=destination,
                depart_date=recommended_flight_date,
                return_date=return_date,
                trip_type="roundtrip",
                adults=adults,
            )
            flight_url = self._require_page().url
            # Prefer canonical hk.trip.com link from tool text
            canon_m = re.search(r"Canonical search URL:\s*(\S+)", flight_text)
            if canon_m:
                recommended_flight_url = canon_m.group(1).rstrip(".,;")
            else:
                recommended_flight_url = flight_url or recommended_flight_url
            recommended_flight_url = ensure_locale_curr(
                normalize_trip_url(recommended_flight_url)
            )
            if "hk.trip.com" not in recommended_flight_url and canon_m:
                recommended_flight_url = ensure_locale_curr(
                    normalize_trip_url(canon_m.group(1).rstrip(".,;"))
                )
            flight_prices = summarize_prices(
                f"Flights {origin}->{destination} on {recommended_flight_date}",
                flight_text,
                url=flight_url,
            )
            if recommended_flight_price is None:
                recommended_flight_price = flight_prices.get("lowest_hkd")
            flight_low = recommended_flight_price
            flight_snippets = flight_prices.get("snippets") or []
            if flight_snippets:
                recommended_flight_option = flight_snippets[0]
            recommended_flight_airline = ""
            airline_m = re.search(
                r"(?im)^-\s*Airline:\s*(.+)$",
                flight_text or "",
            )
            if airline_m:
                cand = airline_m.group(1).strip()
                if is_plausible_airline_name(cand):
                    recommended_flight_airline = cand
            flight_card_fields = extract_booking_urls(flight_text or "")
            if flight_card_fields.get("flight_airline") and is_plausible_airline_name(
                flight_card_fields["flight_airline"]
            ):
                recommended_flight_airline = flight_card_fields["flight_airline"]
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
            # Keep the full structured card (outbound + return) near the top of the tool text
            structured_m = re.search(
                r"(?is)(Structured flight card:\s*.*?)(?:\n\n|Flight search URL:|$)",
                flight_text or "",
            )
            structured_flight_card = (
                structured_m.group(1).strip() if structured_m else ""
            )
            sections.append(
                f"""{n}) FLIGHTS (compared live)
- Recommended depart date: {recommended_flight_date}
- Recommended flight link: {recommended_flight_url}
{airline_line}- Lowest seen for that date: {_fmt_hkd(recommended_flight_price)}
- Compared outbound dates: {", ".join(date_options)}
- Best row: {flight_best_label or "see ranking below"}
- Sample options seen:
{snip_block}
- Ranking:
{flight_compare_text.split("Notes:")[0].strip()}"""
            )
            booking_lines.append(f"  - Recommended flights: {recommended_flight_url}")
            if structured_flight_card:
                raw_blocks.insert(0, structured_flight_card)
            raw_blocks.append("--- Raw flight excerpt ---\n" + flight_text[:3500])
            n += 1

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

        hotel_alt_checkins = nearby_dates(depart_date, (-7, 0, 7))
        hotel_compare_text = self.compare_hotel_prices(
            city=hotel_city,
            checkin=depart_date,
            checkout=return_date,
            adults=adults,
            rooms=1,
            alternate_checkins=",".join(hotel_alt_checkins),
        )
        best_hotel = pick_cheapest_from_comparison(hotel_compare_text)
        hotel_best_label = best_hotel.get("label") or ""
        recommended_hotel_checkin = best_hotel.get("checkin") or depart_date
        recommended_hotel_price = best_hotel.get("lowest_hkd")
        recommended_hotel_url = best_hotel.get("url") or ""
        recommended_hotel_option = ""
        recommended_hotel_detail_links: list[str] = []

        # Open the cheapest check-in window and capture listing + detail links
        try:
            rec_checkout = (
                date.fromisoformat(recommended_hotel_checkin) + timedelta(days=nights)
            ).isoformat()
        except ValueError:
            rec_checkout = return_date

        hotel_text = self.search_hotels(
            city=hotel_city,
            checkin=recommended_hotel_checkin,
            checkout=rec_checkout,
            adults=adults,
            rooms=1,
        )
        hotel_url = self._require_page().url
        canon_h = re.search(r"Canonical search URL:\s*(\S+)", hotel_text)
        if canon_h:
            recommended_hotel_url = canon_h.group(1).rstrip(".,;")
        else:
            recommended_hotel_url = hotel_url or recommended_hotel_url
        recommended_hotel_url = ensure_locale_curr(
            normalize_trip_url(recommended_hotel_url)
        )
        hotel_prices = summarize_prices(
            f"Hotels in {hotel_city}",
            hotel_text,
            url=hotel_url,
        )
        if recommended_hotel_price is None:
            recommended_hotel_price = hotel_prices.get("lowest_hkd")
        hotel_low = recommended_hotel_price
        hotel_total = (
            round(hotel_low * nights, 2) if hotel_low is not None else None
        )
        hotel_snippets = hotel_prices.get("snippets") or []
        if hotel_snippets:
            recommended_hotel_option = hotel_snippets[0]
        recommended_hotel_detail_links = []
        recommended_hotel_names: list[str] = []
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
            recommended_hotel_detail_links.append(nu)
            if (
                label
                and label not in recommended_hotel_names
                and "sample" not in label.lower()
                and "rates range" not in label.lower()
            ):
                recommended_hotel_names.append(label)
            if len(recommended_hotel_detail_links) >= 3:
                break
        # Prefer real hotel title over generic price snippets
        if recommended_hotel_names:
            recommended_hotel_option = recommended_hotel_names[0]

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
- Recommended check-in: {recommended_hotel_checkin} ({nights} nights)
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

        car_low = None
        car_total = None
        if need_car:
            car_text = self.search_cars(
                location=hotel_city,
                pickup_date=depart_date,
                dropoff_date=return_date,
            )
            car_url = self._require_page().url
            car_prices = summarize_prices(
                f"Car rental in {hotel_city}",
                car_text,
                url=car_url,
            )
            car_low = car_prices.get("lowest_hkd")
            car_total = (
                round(car_low * nights, 2) if car_low is not None else None
            )
            sections.append(
                f"""{n}) CAR RENTAL
- Car rental search URL: {car_url}
- Open results: [View car rentals on Trip.com]({car_url})
- Lowest daily seen: {_fmt_hkd(car_low)}
- Est. rental total (lowest x nights): {_fmt_hkd(car_total)}
- Sample prices: {car_prices.get("prices_hkd", [])[:8]}"""
            )
            booking_lines.append(f"  - Cars: {car_url}")
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
        )
        sections.append(
            f"""{n}) SUGGESTED DAY FLOW
{day_lines}"""
        )
        if attractions:
            sections.append(
                "Attraction sources (Trip.com things-to-do):\n"
                + "\n".join(f"- {a}" for a in attractions[:12])
            )
        n += 1

        pick_lines = [
            "======= YOUR RECOMMENDED BOOKINGS =======",
        ]
        if want_flights:
            pick_lines.append("RECOMMENDED FLIGHT")
            pick_lines.append(
                f"- Route: {origin} -> {destination} (round-trip)"
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
        pick_lines.append(f"- City: {hotel_city}")
        pick_lines.append(
            f"- Check-in: {recommended_hotel_checkin} ({nights} nights)"
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
        page = self._require_page()
        try:
            # Destination / city field
            city_box = page.locator(
                "input[placeholder*='City' i], "
                "input[placeholder*='Destination' i], "
                "input[placeholder*='Hotel' i], "
                "input[aria-label*='City' i], "
                "input[aria-label*='Destination' i]"
            ).first
            if not city_box.count():
                city_box = page.locator("input").first
            city_box.click(timeout=5000)
            city_box.fill("")
            city_box.fill(city)
            page.wait_for_timeout(1000)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

            search_btn = page.get_by_role("button", name=re.compile(r"search", re.I))
            if search_btn.count():
                search_btn.first.click()
                page.wait_for_timeout(4000)
                return True

            # Fallback: press Enter in the city field
            city_box.press("Enter")
            page.wait_for_timeout(4000)
            return True
        except Exception:
            return False

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
        picked = _pick_flight_row(filtered)
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
        picked = _pick_flight_row(rows, lowest=lowest)
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

    def scrape_hotel_image_url(self, detail_url: str = "") -> str:
        """Return the main hotel photo URL from a Trip.com hotel detail page."""
        page = self._require_page()
        if detail_url and "trip.com" in detail_url.lower():
            try:
                self._safe_goto(detail_url)
                page.wait_for_timeout(3500)
                self._dismiss_popups()
                # If Trip.com bounced to booknew, try to recover overview meta from there
            except Exception:
                return ""

        # Prefer Open Graph cover image
        try:
            og = page.locator("meta[property='og:image']")
            if og.count():
                content = (og.first.get_attribute("content") or "").strip()
                if content.startswith("http") and "tripcdn.com" in content.lower():
                    return _prefer_hotel_photo_url(content)
        except Exception:
            pass

        # Prefer dedicated overview / gallery hero image
        for sel in (
            "img[alt*='hotel overview' i]",
            "img[alt*='overview picture' i]",
            "img[alt*='Hotel' i]",
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
                if src.startswith("http") and "tripcdn.com" in src.lower():
                    if any(x in src.lower() for x in ("logo", "icon", "avatar", "qrcode")):
                        continue
                    return _prefer_hotel_photo_url(src)

        # Largest on-page TripCDN photo
        try:
            imgs = page.evaluate(
                """() => Array.from(document.querySelectorAll('img')).map(e => ({
                  src: e.currentSrc || e.src || '',
                  w: e.naturalWidth || e.width || 0,
                  h: e.naturalHeight || e.height || 0
                })).filter(x => x.src && x.src.startsWith('http'))"""
            )
        except Exception:
            imgs = []
        best = ""
        best_area = 0
        for im in imgs or []:
            src = str(im.get("src") or "")
            low = src.lower()
            if "tripcdn.com" not in low and "ak-d.tripcdn" not in low:
                continue
            if any(x in low for x in ("logo", "icon", "avatar", "qrcode", "badge")):
                continue
            area = int(im.get("w") or 0) * int(im.get("h") or 0)
            if area > best_area:
                best_area = area
                best = _prefer_hotel_photo_url(src)
        return best if best_area >= 40_000 else ""

    def _scrape_top_hotel_card(
        self,
        *,
        city: str = "",
        fallback_name: str = "",
        lowest: float | None = None,
    ) -> dict[str, str]:
        """Best-effort parse of the first hotel listing on the current page."""
        page = self._require_page()
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        blob = body[:10000]
        card: dict[str, str] = {
            "name": fallback_name or "",
            "stars": "",
            "score": "",
            "score_label": "",
            "reviews": "",
            "location": city or "",
            "price_label": "",
        }

        # Prefer anchor text from detail links
        if not card["name"] or "sample" in card["name"].lower():
            for _url, label in self._extract_hotel_detail_options(limit=5):
                if (
                    label
                    and "sample" not in label.lower()
                    and "rates range" not in label.lower()
                    and 3 < len(label) < 80
                ):
                    card["name"] = label
                    break

        score_m = re.search(r"\b([89](?:\.\d)?|10(?:\.0)?)\b", blob)
        if score_m:
            card["score"] = score_m.group(1)
            try:
                val = float(card["score"])
                card["score_label"] = (
                    "Great" if val >= 9 else "Very Good" if val >= 8 else "Good"
                )
            except ValueError:
                card["score_label"] = "Guest rating"

        stars_m = re.search(r"(?i)([1-5])\s*[- ]?star", blob)
        if stars_m:
            card["stars"] = stars_m.group(1)
        else:
            glyphs = blob.count("★") + blob.count("⭐")
            if 1 <= glyphs <= 5:
                card["stars"] = str(glyphs)

        rev_m = re.search(r"(?i)(\d[\d,]*)\s*reviews?", blob)
        if rev_m:
            card["reviews"] = f"{rev_m.group(1)} reviews"

        if lowest is not None:
            card["price_label"] = f"HK${lowest:,.0f}"
        else:
            prices = [p for p in parse_prices(blob) if p >= 200]
            if prices:
                card["price_label"] = f"HK${min(prices):,.0f}"

        # Cover photo from the listing card (avoid leaving the list page)
        try:
            overview = page.locator("img[alt*='hotel overview' i]")
            if overview.count():
                src = (
                    overview.first.get_attribute("src")
                    or overview.first.evaluate("e => e.currentSrc || ''")
                    or ""
                ).strip()
                if src.startswith("http") and "tripcdn.com" in src.lower():
                    card["image_url"] = _prefer_hotel_photo_url(src)
        except Exception:
            pass

        if not card["name"] or "sample" in card["name"].lower():
            card["name"] = f"Hotels in {city}" if city else "Recommended hotel"
        return card

    def _extract_hotel_detail_options(self, limit: int = 8) -> list[tuple[str, str]]:
        """Return (url, hotel_name) pairs from hotel list page anchors."""
        page = self._require_page()
        found: list[tuple[str, str]] = []
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
                # Prefer short title-like labels; drop CTA-only text
                if (
                    raw_label
                    and 3 < len(raw_label) < 90
                    and "http" not in raw_label.lower()
                    and not re.fullmatch(
                        r"(?i)(see details?|book|select|view|check availability|>)+",
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
            "description": "Search hotels on Trip.com Hong Kong by city or area keyword.",
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
                "Search car rentals on Trip.com Hong Kong. Use when the traveler needs a "
                "rental car, road trip, or self-drive option at the destination."
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
                "Search Trip.com Hong Kong things-to-do "
                "(https://hk.trip.com/things-to-do/?locale=en-HK&curr=HKD) "
                "and return named attractions/experiences for a city. "
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
            "name": "plan_trip",
            "description": (
                "Create a full travel plan on Trip.com: live-compare flight dates and "
                "hotel check-in dates, pick recommended flight + hotel with HKD prices, "
                "compare trains/transfers, optional car rental, budget, and itinerary. "
                "Prefer this for 'plan my trip' requests. Does the comparisons for the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure airport/city code"},
                    "destination": {
                        "type": "string",
                        "description": "Destination airport/city code",
                    },
                    "depart_date": {"type": "string", "description": "Outbound YYYY-MM-DD"},
                    "return_date": {"type": "string", "description": "Return YYYY-MM-DD"},
                    "adults": {"type": "integer", "description": "Number of adults"},
                    "hotel_city": {
                        "type": "string",
                        "description": "City name for hotel search if different from destination code",
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
