"""Unit test for Trip.com flight row text parser.

Reads the fixture captured by scripts/debug_flight_scrape.py.
Run that script first to refresh the fixture if it is missing or stale.
"""

from __future__ import annotations

from pathlib import Path

from travel_agent.browser_tools import parse_trip_com_flight_rows

_FIXTURE = Path(__file__).resolve().parents[1] / "scripts" / "debug_flight_body.txt"


def main() -> int:
    if not _FIXTURE.exists():
        print(f"SKIP — fixture not found: {_FIXTURE}")
        print("Run  python scripts/debug_flight_scrape.py  to generate it.")
        return 0

    body = _FIXTURE.read_text(encoding="utf-8")
    rows = parse_trip_com_flight_rows(body, origin="HKG", destination="PAR")
    assert len(rows) >= 5, rows
    first = rows[0]
    print("first row:", first)
    assert first["airline"] == "Etihad Airways", first
    assert first["depart_time"] == "20:10", first
    assert first["arrive_time"] == "07:55", first
    assert first["depart_airport"] == "HKG", first
    assert first["arrive_airport"] == "CDG", first
    assert "17h" in first["duration"], first
    assert "5,16" in first["price_label"], first
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
