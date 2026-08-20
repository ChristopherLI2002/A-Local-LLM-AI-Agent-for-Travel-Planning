"""50 reasonable trip scenarios for local LLM travel agent logic tests.

Each example is a realistic planner input (destination, nights, styles, budget).
Run:  python -m tests.run_trip_examples
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


def _depart(days_ahead: int = 14) -> str:
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def _return(depart: str, nights: int) -> str:
    return (date.fromisoformat(depart) + timedelta(days=nights)).isoformat()


@dataclass(frozen=True)
class TripExample:
    """One realistic trip the GUI / agent might receive."""

    id: int
    name: str
    destination: str
    origin: str = "Hong Kong"
    nights: int = 7
    styles: tuple[str, ...] = ("First-time",)
    budget_hkd: float = 12000
    rent_car: bool = False
    # Expectations
    expect_regional: bool = False
    expect_min_stays: int = 1
    expect_open_jaw: bool = False
    expect_flight_code: str = ""  # arrive airport / city code substring
    expect_hotel_city_id: bool = True  # known Trip.com hotel city id
    notes: str = ""
    depart_offset_days: int = 14

    def depart(self) -> str:
        return _depart(self.depart_offset_days)

    def ret(self) -> str:
        return _return(self.depart(), self.nights)


# 50 varied, realistic scenarios covering cities, regions, lengths, and styles
EXAMPLES: list[TripExample] = [
    # --- Classic Asia hubs ---
    TripExample(1, "Tokyo first-timer week", "Tokyo", nights=7, styles=("First-time", "Food")),
    TripExample(2, "Seoul culture long weekend", "Seoul", nights=4, styles=("Culture",), budget_hkd=8000),
    TripExample(3, "Taipei food trip", "Taipei", nights=5, styles=("Food", "First-time"), budget_hkd=7000),
    TripExample(4, "Singapore family", "Singapore", nights=5, styles=("Family",), budget_hkd=10000),
    TripExample(5, "Bangkok relaxed", "Bangkok", nights=6, styles=("Relaxed", "Food"), budget_hkd=6000),
    TripExample(6, "Osaka adventure", "Osaka", nights=5, styles=("Adventure", "Food"), budget_hkd=9000),
    TripExample(7, "Shanghai business-leisure", "Shanghai", nights=4, styles=("Culture",), budget_hkd=8500),
    TripExample(8, "Beijing classic", "Beijing", nights=6, styles=("First-time", "Culture"), budget_hkd=9000),
    TripExample(9, "Macau weekend", "Macau", nights=2, styles=("Relaxed",), budget_hkd=4000, depart_offset_days=7),
    TripExample(10, "Shenzhen quick", "Shenzhen", nights=3, styles=("Food",), budget_hkd=3500, depart_offset_days=5),
    # --- Europe ---
    TripExample(11, "Paris honeymoon-ish", "Paris", nights=7, styles=("Relaxed", "Culture"), budget_hkd=18000),
    TripExample(12, "London theatre week", "London", nights=6, styles=("Culture", "Food"), budget_hkd=16000),
    TripExample(13, "Rome first-time", "Rome", nights=5, styles=("First-time", "Culture"), budget_hkd=14000),
    TripExample(14, "Barcelona food", "Barcelona", nights=5, styles=("Food", "Relaxed"), budget_hkd=13000),
    TripExample(15, "Amsterdam short", "Amsterdam", nights=4, styles=("Culture",), budget_hkd=12000),
    # --- Middle East / Oceania / Americas cities ---
    TripExample(16, "Dubai luxury long weekend", "Dubai", nights=4, styles=("First-time",), budget_hkd=15000),
    TripExample(17, "Sydney summer week", "Sydney", nights=7, styles=("Adventure", "Relaxed"), budget_hkd=20000),
    TripExample(18, "New York city break", "New York", nights=5, styles=("First-time", "Food"), budget_hkd=22000),
    TripExample(19, "San Francisco only", "San Francisco", nights=5, styles=("Food", "Culture"), budget_hkd=16000),
    TripExample(20, "Los Angeles only", "Los Angeles", nights=5, styles=("First-time", "Relaxed"), budget_hkd=15000),
    TripExample(21, "San Diego beach", "San Diego", nights=4, styles=("Relaxed", "Family"), budget_hkd=12000),
    # --- SE Asia / nearby ---
    TripExample(22, "Manila family", "Manila", nights=5, styles=("Family",), budget_hkd=7000),
    TripExample(23, "Kuala Lumpur food", "Kuala Lumpur", nights=4, styles=("Food",), budget_hkd=5500),
    TripExample(24, "Jakarta business + food", "Jakarta", nights=4, styles=("Food",), budget_hkd=6000),
    TripExample(25, "Bali adventure", "Bali", nights=7, styles=("Adventure", "Relaxed"), budget_hkd=10000, rent_car=True),
    TripExample(26, "Guangzhou weekend", "Guangzhou", nights=3, styles=("Food",), budget_hkd=4000),
    # --- Regional California (open-jaw / multi-hotel) ---
    TripExample(
        27,
        "California 7-night loop",
        "California",
        nights=7,
        styles=("First-time", "Food"),
        budget_hkd=25000,
        expect_regional=True,
        expect_min_stays=2,
        expect_open_jaw=True,
        expect_flight_code="sfo",
        notes="SF then LA; SFO in / LAX out",
    ),
    TripExample(
        28,
        "California short 3 nights",
        "California",
        nights=3,
        styles=("Adventure",),
        budget_hkd=14000,
        expect_regional=True,
        expect_min_stays=2,
        expect_open_jaw=True,
    ),
    TripExample(
        29,
        "California long 10 nights",
        "California",
        nights=10,
        styles=("First-time", "Relaxed"),
        budget_hkd=32000,
        expect_regional=True,
        expect_min_stays=3,
        expect_open_jaw=True,
        notes="SF + LA + San Diego",
    ),
    TripExample(
        30,
        "CA alias",
        "CA",
        nights=6,
        styles=("Culture",),
        budget_hkd=20000,
        expect_regional=True,
        expect_min_stays=2,
        expect_open_jaw=True,
    ),
    TripExample(
        31,
        "West Coast USA phrasing",
        "West Coast USA",
        nights=8,
        styles=("First-time",),
        budget_hkd=28000,
        expect_regional=True,
        expect_min_stays=2,
        expect_open_jaw=True,
    ),
    # --- Length extremes ---
    TripExample(32, "Tokyo overnight", "Tokyo", nights=1, styles=("First-time",), budget_hkd=4000, depart_offset_days=10),
    TripExample(33, "Seoul 2 nights", "Seoul", nights=2, styles=("Food",), budget_hkd=4500),
    TripExample(34, "Paris 14-night stay", "Paris", nights=14, styles=("Relaxed", "Culture"), budget_hkd=35000),
    TripExample(35, "London 10 nights family", "London", nights=10, styles=("Family",), budget_hkd=28000),
    # --- Style mixes ---
    TripExample(36, "Tokyo adventure food", "Tokyo", nights=6, styles=("Adventure", "Food"), budget_hkd=11000),
    TripExample(37, "Seoul culture food", "Seoul", nights=5, styles=("Culture", "Food"), budget_hkd=9000),
    TripExample(38, "Singapore relaxed family", "Singapore", nights=6, styles=("Relaxed", "Family"), budget_hkd=12000),
    TripExample(39, "Bangkok first-time culture", "Bangkok", nights=7, styles=("First-time", "Culture"), budget_hkd=8000),
    TripExample(40, "Taipei relaxed", "Taipei", nights=4, styles=("Relaxed",), budget_hkd=6500),
    # --- Budget bands ---
    TripExample(41, "Osaka thrifty", "Osaka", nights=4, styles=("Food",), budget_hkd=5000),
    TripExample(42, "Dubai mid budget", "Dubai", nights=5, styles=("First-time",), budget_hkd=12000),
    TripExample(43, "Sydney premium", "Sydney", nights=8, styles=("Relaxed",), budget_hkd=30000),
    TripExample(44, "New York premium food", "New York", nights=6, styles=("Food", "Culture"), budget_hkd=28000),
    # --- Alternate origins (still HK-default app but origin field set) ---
    TripExample(45, "From Macau to Tokyo", "Tokyo", origin="Macau", nights=5, styles=("First-time",), budget_hkd=9000),
    TripExample(46, "From Shenzhen to Taipei", "Taipei", origin="Shenzhen", nights=4, styles=("Food",), budget_hkd=6000),
    TripExample(47, "From Singapore hub to Seoul", "Seoul", origin="Singapore", nights=5, styles=("Culture",), budget_hkd=10000),
    # --- Near-term / farther departures ---
    TripExample(48, "Soon Bangkok", "Bangkok", nights=5, styles=("Relaxed",), budget_hkd=7000, depart_offset_days=3),
    TripExample(49, "Far-out Paris", "Paris", nights=7, styles=("Culture",), budget_hkd=17000, depart_offset_days=60),
    TripExample(
        50,
        "California foodie week",
        "California",
        nights=7,
        styles=("Food", "First-time"),
        budget_hkd=26000,
        expect_regional=True,
        expect_min_stays=2,
        expect_open_jaw=True,
        rent_car=True,
        notes="Food focus + optional car for coastal drive",
    ),
]


assert len(EXAMPLES) == 50, f"Expected 50 examples, got {len(EXAMPLES)}"
assert len({e.id for e in EXAMPLES}) == 50, "Example ids must be unique"
