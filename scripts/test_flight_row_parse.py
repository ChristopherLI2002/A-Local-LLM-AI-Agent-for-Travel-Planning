"""Unit test for Trip.com flight row text parser."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_agent.browser_tools import parse_trip_com_flight_rows

BODY = (ROOT / "scripts" / "debug_flight_body.txt").read_text(encoding="utf-8")


def main() -> int:
    rows = parse_trip_com_flight_rows(BODY, origin="HKG", destination="PAR")
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
