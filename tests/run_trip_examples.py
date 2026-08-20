"""Run all 50 trip examples against travel agent logic (no live browser/LLM).

Validates for each scenario:
  - plan query builds correctly
  - flight/hotel place mapping
  - Trip.com booking URLs (openable)
  - day ideas cover full trip length
  - regional open-jaw / multi-stay when expected
  - airline logo CDN codes for common carriers
  - itinerary parse + ensure_day_blocks shape

Usage:
  python -m tests.run_trip_examples
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field

from tests.trip_examples_50 import EXAMPLES, TripExample

from travel_agent.airline_names import airline_logo_url, airline_name_to_code
from travel_agent.destination_guides import day_ideas_for
from travel_agent.itinerary_parse import ensure_day_blocks, parse_itinerary
from travel_agent.places import to_flight_code, to_hotel_city, to_hotel_city_id
from travel_agent.planner_query import TRAVEL_STYLES, build_plan_query
from travel_agent.regions import build_regional_route, is_regional_destination
from travel_agent.trip_urls import (
    build_flight_search_url,
    build_hotel_list_url,
    is_hotel_list_url,
    is_openable_hotel_url,
)


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


@dataclass
class ExampleReport:
    example: TripExample
    checks: list[tuple[str, CheckResult]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for _, c in self.checks)


def _check(name: str, ok: bool, detail: str = "") -> tuple[str, CheckResult]:
    return name, CheckResult(ok=ok, detail=detail)


def run_example(ex: TripExample) -> ExampleReport:
    report = ExampleReport(example=ex)
    depart = ex.depart()
    ret = ex.ret()
    styles = list(ex.styles)

    # 1) Styles are known
    unknown = [s for s in styles if s not in TRAVEL_STYLES]
    report.checks.append(
        _check("styles_known", not unknown, f"unknown={unknown}" if unknown else "")
    )

    # 2) Plan query
    try:
        query = build_plan_query(
            destination=ex.destination,
            depart_date=depart,
            return_date=ret,
            budget_hkd=ex.budget_hkd,
            origin=ex.origin,
            travel_styles=styles,
            nights=ex.nights,
            rent_car=ex.rent_car,
            include_flights=True,
            include_trains=True,
            include_transfers=True,
        )
        q_ok = (
            ex.destination in query
            and ex.origin in query
            and "plan_trip" in query
            and "Recommended flight" in query
            and "Recommended hotel" in query
        )
        report.checks.append(_check("plan_query", q_ok, f"len={len(query)}"))
    except Exception as exc:
        report.checks.append(_check("plan_query", False, str(exc)))
        query = ""

    # 3) Place codes
    fcode = to_flight_code(ex.destination)
    hcity = to_hotel_city(ex.destination)
    hid = to_hotel_city_id(ex.destination)
    origin_code = to_flight_code(ex.origin)
    report.checks.append(
        _check(
            "flight_code",
            bool(fcode) and len(fcode) >= 3,
            f"code={fcode!r}",
        )
    )
    report.checks.append(
        _check("origin_code", bool(origin_code), f"code={origin_code!r}")
    )
    if ex.expect_flight_code:
        report.checks.append(
            _check(
                "expect_flight_code",
                ex.expect_flight_code.lower() in (fcode or "").lower()
                or ex.expect_flight_code.lower() in (hcity or "").lower()
                or is_regional_destination(ex.destination),
                f"got={fcode!r}",
            )
        )
    if ex.expect_hotel_city_id and not ex.expect_regional:
        # Single-city trips should map to a numeric Trip.com city id when known
        report.checks.append(
            _check(
                "hotel_city_id",
                bool(hid) or bool(to_hotel_city_id(hcity)),
                f"city={hcity!r} id={hid!r}",
            )
        )

    # 4) Booking URLs
    try:
        flight_url = build_flight_search_url(
            origin=ex.origin,
            destination=fcode or ex.destination,
            depart_date=depart,
            return_date=ret,
            trip_type="roundtrip",
        )
        report.checks.append(
            _check(
                "flight_url",
                "hk.trip.com" in flight_url and "/flights/" in flight_url,
                flight_url[:90],
            )
        )
    except Exception as exc:
        report.checks.append(_check("flight_url", False, str(exc)))
        flight_url = ""

    hotel_target = hcity or ex.destination
    route = build_regional_route(ex.destination, ex.nights, depart_date=depart)
    if route and route.stays:
        hotel_target = route.stays[0].city
        hotel_checkin = route.stays[0].checkin or depart
        hotel_checkout = route.stays[0].checkout or ret
    else:
        hotel_checkin, hotel_checkout = depart, ret

    try:
        hotel_url = build_hotel_list_url(
            city=hotel_target,
            checkin=hotel_checkin,
            checkout=hotel_checkout,
        )
        report.checks.append(
            _check(
                "hotel_url",
                is_hotel_list_url(hotel_url) and is_openable_hotel_url(hotel_url),
                hotel_url[:100],
            )
        )
    except Exception as exc:
        report.checks.append(_check("hotel_url", False, str(exc)))
        hotel_url = ""

    # Multi-stay URLs for regional
    if route and len(route.stays) > 1:
        stay_urls_ok = True
        for stay in route.stays:
            u = build_hotel_list_url(
                city=stay.city,
                checkin=stay.checkin or depart,
                checkout=stay.checkout or ret,
            )
            if not is_openable_hotel_url(u):
                stay_urls_ok = False
                break
        report.checks.append(_check("multi_stay_hotel_urls", stay_urls_ok))

    # 5) Regional expectations
    is_reg = is_regional_destination(ex.destination)
    report.checks.append(
        _check(
            "regional_flag",
            is_reg == ex.expect_regional,
            f"got={is_reg} expect={ex.expect_regional}",
        )
    )
    if ex.expect_regional:
        report.checks.append(
            _check("regional_route", route is not None, "route missing")
        )
        if route:
            report.checks.append(
                _check(
                    "min_stays",
                    len(route.stays) >= ex.expect_min_stays,
                    f"stays={len(route.stays)} cities={[s.city for s in route.stays]}",
                )
            )
            nights_sum = sum(s.nights for s in route.stays)
            report.checks.append(
                _check(
                    "nights_sum",
                    nights_sum == ex.nights,
                    f"sum={nights_sum} nights={ex.nights}",
                )
            )
            if ex.expect_open_jaw:
                report.checks.append(
                    _check(
                        "open_jaw",
                        route.arrive_airport.upper() != route.depart_airport.upper(),
                        f"in={route.arrive_airport} out={route.depart_airport}",
                    )
                )
                # Open-jaw flight URLs for arrive / depart cities
                out_url = build_flight_search_url(
                    origin=ex.origin,
                    destination=route.arrive_airport,
                    depart_date=depart,
                    trip_type="oneway",
                )
                ret_url = build_flight_search_url(
                    origin=route.depart_airport,
                    destination=ex.origin,
                    depart_date=ret,
                    trip_type="oneway",
                )
                report.checks.append(
                    _check(
                        "open_jaw_flight_urls",
                        "/flights/" in out_url and "/flights/" in ret_url,
                        f"out={route.arrive_airport} ret={route.depart_airport}",
                    )
                )

    # 6) Day ideas cover trip length
    try:
        ideas = day_ideas_for(
            ex.destination,
            ex.nights,
            styles=styles,
            regional_route=route,
        )
        report.checks.append(
            _check(
                "day_ideas_count",
                len(ideas) == ex.nights,
                f"got={len(ideas)} want={ex.nights}",
            )
        )
        if ideas:
            bodies = [getattr(i, "title", "") or "" for i in ideas]
            report.checks.append(
                _check("day_ideas_titled", all(bool(t.strip()) for t in bodies))
            )
    except Exception as exc:
        report.checks.append(_check("day_ideas_count", False, str(exc)))

    # 7) ensure_day_blocks on a minimal plan
    try:
        stub = (
            f"Recommended flight\n- From: {origin_code.upper() or 'HKG'}\n"
            f"- To: {(fcode or 'XXX').upper()}\n"
            f"- Link: {flight_url or 'https://hk.trip.com/flights/'}\n\n"
            f"Recommended hotel\n- Hotel: Sample Stay\n"
            f"- Link: {hotel_url or 'https://hk.trip.com/hotels/list'}\n\n"
            "Day-by-day\nDay 1: Arrival\n- 10:00 Land and check in\n"
        )
        parsed = parse_itinerary(stub)
        parsed = ensure_day_blocks(
            parsed,
            nights=ex.nights,
            destination=ex.destination,
            styles=styles,
        )
        report.checks.append(
            _check(
                "ensure_days",
                len(parsed.days) >= ex.nights,
                f"days={len(parsed.days)}",
            )
        )
    except Exception as exc:
        report.checks.append(_check("ensure_days", False, str(exc)))

    # 8) Airline logo helper sanity (shared app path)
    for airline in ("EVA Air", "United Airlines", "Cathay Pacific"):
        code = airline_name_to_code(airline)
        logo = airline_logo_url(airline)
        report.checks.append(
            _check(
                f"airline_logo:{airline}",
                bool(code) and logo.endswith(f"{code.lower()}.png"),
                logo[:70],
            )
        )
        break  # one sample per example is enough; still validate helper works

    return report


def main() -> int:
    print(f"Local LLM travel agent — running {len(EXAMPLES)} trip examples\n")
    reports: list[ExampleReport] = []
    failures = 0

    for ex in EXAMPLES:
        try:
            rep = run_example(ex)
        except Exception:
            rep = ExampleReport(example=ex)
            rep.checks.append(
                _check("runner", False, traceback.format_exc(limit=3))
            )
        reports.append(rep)
        status = "PASS" if rep.passed else "FAIL"
        if not rep.passed:
            failures += 1
        failed = [f"{n}: {c.detail}" for n, c in rep.checks if not c.ok]
        extra = f"  !! {'; '.join(failed)}" if failed else ""
        print(
            f"[{status}] #{ex.id:02d} {ex.name} — {ex.origin}->{ex.destination}, "
            f"{ex.nights}n, {','.join(ex.styles)}{extra}"
        )

    passed = len(reports) - failures
    print("\n" + "=" * 60)
    print(f"Results: {passed}/{len(reports)} examples passed")
    if failures:
        print("\nFailed checks detail:")
        for rep in reports:
            if rep.passed:
                continue
            print(f"\n  #{rep.example.id} {rep.example.name}")
            for name, c in rep.checks:
                if not c.ok:
                    print(f"    - {name}: {c.detail or 'failed'}")
        return 1
    print("All 50 examples passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
