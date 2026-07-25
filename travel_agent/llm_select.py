"""LLM-based flight and hotel selection from scraped Trip.com candidates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import ollama

from travel_agent.config import settings
from travel_agent.pricing import parse_prices


@dataclass
class SelectionContext:
    """Trip preferences passed to the LLM when ranking scraped options."""

    origin: str = ""
    destination: str = ""
    nights: int = 0
    budget_hkd: float | None = None
    interests: str = ""
    travel_styles: str = ""
    checkin: str = ""
    checkout: str = ""
    adults: int = 2

    def summary(self) -> str:
        parts: list[str] = []
        if self.origin and self.destination:
            parts.append(f"Route: {self.origin} → {self.destination}")
        if self.nights:
            parts.append(f"Stay: {self.nights} night(s)")
        if self.checkin and self.checkout:
            parts.append(f"Hotel dates: {self.checkin} → {self.checkout}")
        if self.budget_hkd is not None:
            parts.append(f"Total budget: HK${self.budget_hkd:,.0f}")
        if self.travel_styles:
            parts.append(f"Travel style: {self.travel_styles}")
        elif self.interests:
            parts.append(f"Interests: {self.interests}")
        if self.adults:
            parts.append(f"Adults: {self.adults}")
        return "\n".join(parts) if parts else "General leisure trip from Hong Kong."


def _flight_price(row: dict[str, str]) -> float | None:
    prices = parse_prices(row.get("price_label", ""))
    return prices[0] if prices else None


def _cheapest_flight_index(rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    best_i = 0
    best_p: float | None = None
    for i, row in enumerate(rows):
        p = _flight_price(row)
        if p is None:
            continue
        if best_p is None or p < best_p:
            best_p = p
            best_i = i
    return best_i


def _format_flight_candidates(rows: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for i, row in enumerate(rows[:8]):
        lines.append(
            f"{i}. {row.get('airline', 'Unknown airline')}"
            f" | {row.get('depart_time', '?')} → {row.get('arrive_time', '?')}"
            f" | {row.get('depart_airport', '')} → {row.get('arrive_airport', '')}"
            f" | {row.get('duration', '') or 'duration n/a'}"
            f" | Stops: {row.get('stops', 'n/a')}"
            f" | {row.get('price_label', 'price n/a')}"
        )
    return "\n".join(lines)


def _format_hotel_candidates(candidates: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for i, row in enumerate(candidates[:8]):
        lines.append(
            f"{i}. {row.get('name', 'Unknown hotel')}"
            f" | Stars: {row.get('stars', 'n/a')}"
            f" | Score: {row.get('score', 'n/a')}"
            f" | Reviews: {row.get('reviews', 'n/a')}"
            f" | Location: {row.get('location', 'n/a')}"
            f" | {row.get('price_label', 'price n/a')}"
        )
    return "\n".join(lines)


def _parse_choice_index(raw: str, limit: int) -> int | None:
    """Extract a 0-based index from LLM output."""
    text = (raw or "").strip()
    if not text or limit <= 0:
        return None

    # JSON object: {"index": 2} or {"choice": 1}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("choice", "selected", "pick"):
                if key in data:
                    val = int(data[key])
                    if 1 <= val <= limit:
                        return val - 1
                    if 0 <= val < limit:
                        return val
            if "index" in data:
                val = int(data["index"])
                if 0 <= val < limit:
                    return val
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Bare number
    m = re.search(r"\b(\d+)\b", text)
    if m:
        val = int(m.group(1))
        if 0 <= val < limit:
            return val
        if 1 <= val <= limit:
            return val - 1
    return None


def _llm_pick_index(
    *,
    kind: str,
    candidates_text: str,
    context: SelectionContext,
    model: str | None = None,
    host: str | None = None,
) -> int | None:
    """Ask Ollama to pick one candidate index. Returns None on failure."""
    if not candidates_text.strip():
        return None

    model = model or settings.ollama_model
    host = host or settings.ollama_host
    limit = candidates_text.count("\n") + 1

    system = (
        f"You are a travel concierge selecting the best {kind} from live Trip.com "
        "search results. Reply with ONLY a JSON object: "
        '{"index": <0-based integer>, "reason": "<one short sentence>"}. '
        "Pick ONE option that best matches budget, travel style, comfort, and value — "
        "not always the cheapest. Prefer direct flights when times are reasonable. "
        "Prefer well-reviewed hotels in convenient areas for the stated travel style."
    )
    user = (
        f"Trip context:\n{context.summary()}\n\n"
        f"{kind.title()} options (0-based index):\n{candidates_text}\n\n"
        "Which index is the best pick?"
    )

    try:
        client = ollama.Client(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"temperature": 0.2},
        )
        content = (response.get("message") or {}).get("content") or ""
        return _parse_choice_index(content, limit)
    except Exception:
        return None


def select_flight_row(
    rows: list[dict[str, str]],
    context: SelectionContext | None = None,
    *,
    model: str | None = None,
    host: str | None = None,
    lowest: float | None = None,
) -> dict[str, str] | None:
    """Pick the best flight row using the LLM, with cheapest fallback."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    ctx = context or SelectionContext()
    idx = _llm_pick_index(
        kind="flight",
        candidates_text=_format_flight_candidates(rows),
        context=ctx,
        model=model,
        host=host,
    )
    if idx is None:
        idx = _cheapest_flight_index(rows)
    row = rows[max(0, min(idx, len(rows) - 1))]
    if lowest is not None and not row.get("price_label"):
        row = dict(row)
        row["price_label"] = f"HK${lowest:,.0f}"
    return row


def select_hotel_candidate(
    candidates: list[dict[str, str]],
    context: SelectionContext | None = None,
    *,
    model: str | None = None,
    host: str | None = None,
) -> dict[str, str] | None:
    """Pick the best hotel candidate using the LLM, with first-option fallback."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    ctx = context or SelectionContext()
    idx = _llm_pick_index(
        kind="hotel",
        candidates_text=_format_hotel_candidates(candidates),
        context=ctx,
        model=model,
        host=host,
    )
    if idx is None:
        idx = 0
    return candidates[max(0, min(idx, len(candidates) - 1))]


def enrich_hotel_candidates_from_page(
    options: list[tuple[str, str]],
    page_text: str,
    *,
    city: str = "",
    default_prices: list[float] | None = None,
) -> list[dict[str, str]]:
    """Turn (url, name) pairs into rows the LLM can rank."""
    blob = page_text or ""
    prices = default_prices or [p for p in parse_prices(blob) if p >= 200]
    out: list[dict[str, str]] = []
    for url, name in options:
        row: dict[str, str] = {
            "name": name or f"Hotel in {city}",
            "url": url,
            "stars": "",
            "score": "",
            "reviews": "",
            "location": city,
            "price_label": "",
        }
        if name:
            # Score / stars near the hotel name in listing text
            esc = re.escape(name[:40])
            ctx_m = re.search(
                rf"(?is).{{0,120}}{esc}.{{0,220}}",
                blob,
            )
            chunk = ctx_m.group(0) if ctx_m else ""
            score_m = re.search(r"\b([89](?:\.\d)?|10(?:\.0)?)\b", chunk or blob)
            if score_m:
                row["score"] = score_m.group(1)
            stars_m = re.search(r"(?i)([1-5])\s*[- ]?star", chunk or blob)
            if stars_m:
                row["stars"] = stars_m.group(1)
            rev_m = re.search(r"(?i)(\d[\d,]*)\s*reviews?", chunk or blob)
            if rev_m:
                row["reviews"] = f"{rev_m.group(1)} reviews"
            near_prices = [p for p in parse_prices(chunk) if p >= 200]
            if near_prices:
                row["price_label"] = f"HK${min(near_prices):,.0f}"
        if not row["price_label"] and prices:
            # Assign ascending list prices as a weak signal when row-level parse fails
            idx = len(out)
            if idx < len(prices):
                row["price_label"] = f"HK${prices[idx]:,.0f}"
        out.append(row)
    return out
