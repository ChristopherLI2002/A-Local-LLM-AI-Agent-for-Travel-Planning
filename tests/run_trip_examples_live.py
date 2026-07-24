"""Full live Trip.com scrape for all 50 trip examples (same path as GUI plan_trip).

This drives Playwright via TripBrowser.plan_trip — the tool the desktop agent uses —
for every scenario in trip_examples_50.py. Results are appended after each example
so you can resume with --resume.

Expect ~5–15+ minutes per example (California multi-city is slower). Full suite
often takes several hours.

Usage:
  python -m tests.run_trip_examples_live
  python -m tests.run_trip_examples_live --ids 1,2,27
  python -m tests.run_trip_examples_live --start 10 --limit 5
  python -m tests.run_trip_examples_live --resume
  python -m tests.run_trip_examples_live --show-browser
  python -m tests.run_trip_examples_live --mode scrape   # flights+hotels only (faster)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tests.trip_examples_50 import EXAMPLES, TripExample
from travel_agent.config import settings
from travel_agent.places import to_flight_code
from travel_agent.regions import build_regional_route
from travel_agent.trip_urls import (
    extract_booking_urls,
    is_openable_hotel_url,
    is_trusted_hotel_detail_url,
)

RESULTS_DIR = Path(__file__).resolve().parent / "live_results"
PROGRESS_PATH = RESULTS_DIR / "progress.jsonl"
SUMMARY_PATH = RESULTS_DIR / "summary.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_done_ids() -> set[int]:
    done: set[int] = set()
    if not PROGRESS_PATH.exists():
        return done
    for line in PROGRESS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if row.get("id") is not None:
                done.add(int(row["id"]))
        except Exception:
            continue
    return done


def _append_result(row: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with PROGRESS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _score_plan_text(text: str) -> dict:
    """Heuristic pass/fail signals from plan_trip / scrape output."""
    low = (text or "").lower()
    urls = extract_booking_urls(text or "")
    flight_url = urls.get("flight") or ""
    hotel_url = urls.get("hotel") or ""
    has_price = bool(re.search(r"hk\s*\$\s*\d", text or "", re.I))
    has_canonical = "canonical search url" in low or "hk.trip.com" in low
    has_flight_section = "flight" in low
    has_hotel_section = "hotel" in low
    return {
        "flight_url": flight_url[:300],
        "hotel_url": hotel_url[:300],
        "hotel_name": (urls.get("hotel_name") or "")[:120],
        "has_price": has_price,
        "has_canonical": has_canonical,
        "has_flight_section": has_flight_section,
        "has_hotel_section": has_hotel_section,
        "hotel_openable": is_openable_hotel_url(hotel_url) if hotel_url else False,
        "hotel_detail": is_trusted_hotel_detail_url(hotel_url) if hotel_url else False,
        "chars": len(text or ""),
    }


def _pass_from_signals(signals: dict, *, regional: bool, stays: list) -> tuple[bool, list[str]]:
    fails: list[str] = []
    if signals["chars"] < 200:
        fails.append("output_too_short")
    if not signals["has_flight_section"]:
        fails.append("missing_flight_section")
    if not signals["has_hotel_section"]:
        fails.append("missing_hotel_section")
    if not signals["has_canonical"] and not signals["flight_url"] and not signals["hotel_url"]:
        fails.append("no_trip_com_urls")
    if regional and len(stays) < 2:
        fails.append(f"regional_stays={len(stays)}")
    return (len(fails) == 0, fails)


def scrape_example(browser, ex: TripExample, *, mode: str) -> dict:
    """Run live scrape for one example; returns a result dict."""
    depart = ex.depart()
    ret = ex.ret()
    interests = ", ".join(ex.styles)
    t0 = time.time()
    route = build_regional_route(ex.destination, ex.nights, depart_date=depart)
    stays_out: list[dict] = []
    text = ""
    err = ""

    try:
        if mode == "plan_trip":
            text = browser.plan_trip(
                origin=ex.origin,
                destination=ex.destination,
                depart_date=depart,
                return_date=ret,
                adults=2,
                hotel_city=ex.destination,
                budget_hkd=ex.budget_hkd,
                interests=interests,
                rent_car=ex.rent_car,
                include_flights=True,
                include_trains=True,
                include_transfers=True,
            )
            stays_out = list(getattr(browser, "last_hotel_stays", None) or [])
        else:
            # Faster path: outbound (+ return) flights and hotel list per stay city
            chunks: list[str] = []
            arrive = (
                route.arrive_airport
                if route
                else to_flight_code(ex.destination)
            )
            depart_ap = (
                route.depart_airport
                if route
                else to_flight_code(ex.destination)
            )
            if route and arrive.upper() != depart_ap.upper():
                chunks.append(
                    browser.search_flights(
                        origin=ex.origin,
                        destination=arrive,
                        depart_date=depart,
                        trip_type="oneway",
                        adults=2,
                        include_return_leg=False,
                    )
                )
                chunks.append(
                    browser.search_flights(
                        origin=depart_ap,
                        destination=ex.origin,
                        depart_date=ret,
                        trip_type="oneway",
                        adults=2,
                        include_return_leg=False,
                    )
                )
            else:
                chunks.append(
                    browser.search_flights(
                        origin=ex.origin,
                        destination=arrive or ex.destination,
                        depart_date=depart,
                        return_date=ret,
                        trip_type="roundtrip",
                        adults=2,
                    )
                )
            stay_cities = (
                [(s.city, s.checkin or depart, s.checkout or ret) for s in route.stays]
                if route and route.stays
                else [(ex.destination, depart, ret)]
            )
            for city, cin, cout in stay_cities:
                h = browser.search_hotels(
                    city=city, checkin=cin, checkout=cout, adults=2, rooms=1
                )
                chunks.append(h)
                card = browser._scrape_top_hotel_card(city=city)
                stays_out.append(
                    {
                        "city": city,
                        "checkin": cin,
                        "checkout": cout,
                        "name": card.get("name", ""),
                        "image_url": card.get("image_url", ""),
                        "price_label": card.get("price_label", ""),
                    }
                )
            text = "\n\n".join(chunks)
            browser.last_hotel_stays = stays_out
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        text = text or traceback.format_exc(limit=8)

    elapsed = round(time.time() - t0, 1)
    signals = _score_plan_text(text)
    # Prefer structured stays from browser when present
    if not stays_out:
        stays_out = list(getattr(browser, "last_hotel_stays", None) or [])
    ok, fails = _pass_from_signals(
        signals, regional=bool(ex.expect_regional), stays=stays_out
    )
    if err:
        ok = False
        fails.append("exception")

    # Persist a truncated transcript per example
    transcript_path = RESULTS_DIR / f"ex_{ex.id:02d}.txt"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        (text or "")[:120_000], encoding="utf-8", errors="replace"
    )

    return {
        "id": ex.id,
        "name": ex.name,
        "destination": ex.destination,
        "origin": ex.origin,
        "nights": ex.nights,
        "styles": list(ex.styles),
        "depart": depart,
        "return": ret,
        "mode": mode,
        "ok": ok,
        "fails": fails,
        "error": err,
        "elapsed_sec": elapsed,
        "signals": signals,
        "stays": [
            {
                "city": s.get("city", ""),
                "name": (s.get("name") or "")[:80],
                "nights": s.get("nights", ""),
                "url": (s.get("url") or "")[:200],
                "has_image": bool(s.get("image_url")),
            }
            for s in stays_out
        ],
        "transcript": str(transcript_path.name),
        "finished_at": _utc_now(),
    }


def _select_examples(args: argparse.Namespace) -> list[TripExample]:
    items = list(EXAMPLES)
    if args.ids:
        want = {int(x.strip()) for x in args.ids.split(",") if x.strip()}
        items = [e for e in items if e.id in want]
    if args.start:
        items = [e for e in items if e.id >= args.start]
    if args.limit and args.limit > 0:
        items = items[: args.limit]
    if args.resume:
        done = _load_done_ids()
        items = [e for e in items if e.id not in done]
    return items


def main() -> int:
    # Line-buffered logs when redirected to a file
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Live Trip.com scrape for 50 trip examples")
    parser.add_argument("--mode", choices=("plan_trip", "scrape"), default="plan_trip")
    parser.add_argument("--ids", help="Comma-separated example ids, e.g. 1,27,50")
    parser.add_argument("--start", type=int, default=0, help="Start from this example id")
    parser.add_argument("--limit", type=int, default=0, help="Max examples this run")
    parser.add_argument("--resume", action="store_true", help="Skip ids already in progress.jsonl")
    parser.add_argument("--show-browser", action="store_true", help="Visible Chromium")
    parser.add_argument(
        "--restart-every",
        type=int,
        default=10,
        help="Restart browser every N examples (default 10)",
    )
    args = parser.parse_args()

    if args.show_browser:
        settings.headless = False

    examples = _select_examples(args)
    if not examples:
        print("Nothing to run (empty selection or all resumed).")
        return 0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"Live scrape: {len(examples)} examples | mode={args.mode} | "
        f"headless={settings.headless}",
        flush=True,
    )
    print(f"Results -> {RESULTS_DIR}", flush=True)
    print("This can take several hours for all 50.\n", flush=True)

    from travel_agent.browser_tools import TripBrowser

    browser = TripBrowser()
    browser.start()
    passed = 0
    failed = 0
    results: list[dict] = []

    try:
        for i, ex in enumerate(examples, start=1):
            print(
                f"\n=== [{i}/{len(examples)}] #{ex.id:02d} {ex.name} "
                f"({ex.origin}->{ex.destination}, {ex.nights}n) ===",
                flush=True,
            )
            row = scrape_example(browser, ex, mode=args.mode)
            _append_result(row)
            results.append(row)
            status = "PASS" if row["ok"] else "FAIL"
            if row["ok"]:
                passed += 1
            else:
                failed += 1
            print(
                f"[{status}] {row['elapsed_sec']}s | fails={row['fails'] or '-'} | "
                f"stays={len(row['stays'])} | chars={row['signals']['chars']}",
                flush=True,
            )
            if row.get("error"):
                print(f"  error: {row['error'][:200]}", flush=True)

            # Periodic browser restart to reduce Trip.com session rot
            if args.restart_every > 0 and i % args.restart_every == 0 and i < len(examples):
                print("… restarting browser …", flush=True)
                try:
                    browser.close()
                except Exception:
                    pass
                browser = TripBrowser()
                browser.start()
    finally:
        try:
            browser.close()
        except Exception:
            pass

    summary = {
        "finished_at": _utc_now(),
        "mode": args.mode,
        "ran": len(results),
        "passed": passed,
        "failed": failed,
        "ids_passed": [r["id"] for r in results if r["ok"]],
        "ids_failed": [r["id"] for r in results if not r["ok"]],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"Live scrape done: {passed}/{len(results)} passed")
    if failed:
        print(f"Failed ids: {summary['ids_failed']}")
    print(f"Summary: {SUMMARY_PATH}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
