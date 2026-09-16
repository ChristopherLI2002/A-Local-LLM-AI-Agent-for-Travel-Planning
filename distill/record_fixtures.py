"""Phase 1: scrape each unique destination once into tool_fixtures.jsonl."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Ensure repo root on path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.paths import FIXTURES_STATUS_PATH, TOOL_FIXTURES_PATH, ensure_data_dirs
from distill.prompt_bank import destination_units
from distill.session import SessionRunner


def _scrape_destination(browser: Any, unit: dict[str, Any]) -> dict[str, Any]:
    from travel_agent.regions import build_regional_route

    dest = unit["destination"]
    origin = unit.get("origin") or "Hong Kong"
    nights = int(unit.get("nights") or 7)
    depart = unit["depart"]
    ret = unit["return"]
    interests = ", ".join(unit.get("styles") or [])
    rent_car = bool(unit.get("rent_car"))
    tools: dict[str, str] = {}
    route_text = ""
    plan_text = ""
    attractions_text = ""
    cars_text = ""
    err = ""

    try:
        route_text = browser.propose_trip_route(
            destination=dest,
            nights=nights,
            depart_date=depart,
            origin=origin,
            interests=interests,
        )
        tools["propose_trip_route"] = route_text

        route = getattr(browser, "last_proposed_route", None) or build_regional_route(
            dest, nights, depart_date=depart
        )
        arrive = getattr(route, "arrive_airport", "") if route else ""
        depart_ap = getattr(route, "depart_airport", "") if route else ""
        hotel_city = dest
        if route and getattr(route, "stays", None):
            hotel_city = route.stays[0].city

        plan_text = browser.plan_trip(
            origin=origin,
            destination=dest,
            depart_date=depart,
            return_date=ret,
            adults=2,
            hotel_city=hotel_city,
            budget_hkd=float(unit.get("budget_hkd") or 12000),
            interests=interests,
            rent_car=rent_car,
            include_flights=True,
            include_trains=True,
            include_transfers=True,
            arrive_airport=arrive,
            return_airport=depart_ap,
        )
        tools["plan_trip"] = plan_text

        # Attractions for primary city
        try:
            attractions_text = browser.search_attractions(city=hotel_city, interests=interests)
            tools["search_attractions"] = attractions_text
        except Exception as exc:
            attractions_text = f"[attractions error] {exc}"
            tools["search_attractions"] = attractions_text

        if rent_car:
            try:
                cars_text = browser.search_cars(
                    location=hotel_city,
                    pickup_date=depart,
                    dropoff_date=ret,
                )
                tools["search_cars"] = cars_text
            except Exception as exc:
                cars_text = f"[cars error] {exc}"
                tools["search_cars"] = cars_text

        # Optional: store last flight/hotel card summaries
        flight_card = getattr(browser, "last_plan_flight_card", None) or getattr(
            browser, "last_flight_card", None
        ) or {}
        hotel_stays = list(getattr(browser, "last_hotel_stays", None) or [])

        return {
            "key": unit["key"],
            "ok": True,
            "destination": dest,
            "example_id": unit.get("example_id"),
            "origin": origin,
            "nights": nights,
            "depart": depart,
            "return": ret,
            "rent_car": rent_car,
            "route_text": route_text[:80_000],
            "plan_text": plan_text[:120_000],
            "attractions_text": attractions_text[:60_000],
            "cars_text": cars_text[:40_000],
            "tools": {k: (v or "")[:120_000] for k, v in tools.items()},
            "flight_card": {k: str(v)[:200] for k, v in (flight_card or {}).items()},
            "hotel_stays": [
                {
                    "city": s.get("city", ""),
                    "name": (s.get("name") or "")[:80],
                    "url": (s.get("url") or "")[:300],
                }
                for s in hotel_stays
            ],
            "plan_chars": len(plan_text or ""),
        }
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        return {
            "key": unit["key"],
            "ok": False,
            "destination": dest,
            "error": err,
            "traceback": traceback.format_exc(limit=12),
            "route_text": route_text[:20_000],
            "plan_text": plan_text[:20_000],
            "tools": tools,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Trip.com tool fixtures per destination")
    parser.add_argument("--max-hours", type=float, default=1.0)
    parser.add_argument("--resume", action="store_true", help="Skip destinations already in JSONL")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max destinations this session (0=all)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List units only; do not scrape",
    )
    args = parser.parse_args(argv)

    ensure_data_dirs()
    units = destination_units()
    if args.limit and args.limit > 0:
        units = units[: args.limit]

    print(f"Destinations to scrape: {len(units)}")
    print(f"Progress file: {TOOL_FIXTURES_PATH}")
    if args.dry_run:
        for u in units:
            print(f"  {u['key']}  nights={u['nights']}  {u['destination']}")
        return 0

    if args.show_browser:
        from travel_agent.config import settings

        settings.headless = False

    from travel_agent.browser_tools import TripBrowser
    from travel_agent.llm_select import SelectionContext

    browser = TripBrowser()
    browser.start()

    runner = SessionRunner(
        phase="record_fixtures",
        progress_path=TOOL_FIXTURES_PATH,
        resume_cmd="python -m distill.record_fixtures --resume --max-hours 1",
        max_hours=args.max_hours,
        key_field="key",
        status_path=FIXTURES_STATUS_PATH,
    )
    # --resume is default behavior via done keys; without --resume wipe? Plan says resume
    # Always resume by key (safe). If user wants fresh, delete the JSONL.
    if not args.resume and TOOL_FIXTURES_PATH.exists():
        print("Note: existing fixtures will be skipped by key. Pass --resume explicitly or delete JSONL to redo.")
        # Still skip done keys — same as resume
        pass

    def work(key: str, unit: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        browser.selection_context = SelectionContext(
            origin=unit.get("origin") or "Hong Kong",
            destination=unit["destination"],
            nights=int(unit.get("nights") or 7),
            budget_hkd=float(unit.get("budget_hkd") or 0) or None,
            travel_styles=", ".join(unit.get("styles") or []),
            checkin=unit.get("depart") or "",
            checkout=unit.get("return") or "",
            rent_car=bool(unit.get("rent_car")),
        )
        print(f"\n>>> Scraping {unit['destination']} ({key}) …", flush=True)
        row = _scrape_destination(browser, unit)
        row["elapsed_sec"] = round(time.monotonic() - t0, 2)
        status = "OK" if row.get("ok") else "FAIL"
        print(f"<<< {status} {unit['destination']} in {row['elapsed_sec']}s", flush=True)
        return row

    try:
        unit_pairs = [(u["key"], u) for u in units]
        runner.run(unit_pairs, work, total_planned=len(units), default_unit_sec=600.0)
    finally:
        try:
            browser.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
