"""Unit tests for LLM flight/hotel selection helpers (no live Ollama)."""

from __future__ import annotations

from travel_agent.llm_select import (
    SelectionContext,
    _cheapest_flight_index,
    _parse_choice_index,
    enrich_hotel_candidates_from_page,
    select_flight_row,
)


def test_parse_choice_index_json_zero_based() -> None:
    assert _parse_choice_index('{"index": 2, "reason": "direct"}', 5) == 2


def test_parse_choice_index_json_one_based() -> None:
    assert _parse_choice_index('{"choice": 1}', 4) == 0


def test_parse_choice_index_bare_number() -> None:
    assert _parse_choice_index("Pick option 3", 5) == 3


def test_cheapest_flight_index() -> None:
    rows = [
        {"price_label": "HK$4,500"},
        {"price_label": "HK$3,200"},
        {"price_label": "HK$5,100"},
    ]
    assert _cheapest_flight_index(rows) == 1


def test_select_flight_row_fallback_without_ollama() -> None:
    import travel_agent.llm_select as ls

    original = ls._llm_pick_index
    try:
        ls._llm_pick_index = lambda **_k: None
        rows = [
            {"airline": "CX", "price_label": "HK$4,000"},
            {"airline": "UO", "price_label": "HK$2,800"},
        ]
        picked = select_flight_row(rows, SelectionContext(budget_hkd=12000))
        assert picked is not None
        assert picked["airline"] == "UO"
    finally:
        ls._llm_pick_index = original


def test_enrich_hotel_candidates_from_page() -> None:
    page = (
        "Grand Hyatt Tokyo\n"
        "9.2 Excellent · 1,204 reviews · 5-star\n"
        "HK$2,450 per night\n"
        "Shinjuku Prince Hotel\n"
        "8.4 Very Good · 890 reviews\n"
        "HK$1,120 per night"
    )
    options = [
        ("https://hk.trip.com/hotels/detail/?hotelId=1", "Grand Hyatt Tokyo"),
        ("https://hk.trip.com/hotels/detail/?hotelId=2", "Shinjuku Prince Hotel"),
    ]
    rows = enrich_hotel_candidates_from_page(options, page, city="Tokyo")
    assert len(rows) == 2
    assert rows[0]["score"] == "9.2"
    assert "HK$" in rows[0]["price_label"]
