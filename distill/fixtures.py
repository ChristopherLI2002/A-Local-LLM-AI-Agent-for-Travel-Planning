"""Tool fixture store + replay for teacher rollouts (no live Playwright)."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Callable

from distill.paths import TOOL_FIXTURES_PATH, ensure_data_dirs
from distill.session import load_jsonl

# Price / date jitter patterns
_HKD_RE = re.compile(r"(HK\$?\s*)([0-9][0-9,]*(?:\.\d+)?)", re.I)
_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def normalize_tool_args(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize args for fixture lookup (lowercase cities, strip noise)."""
    a = dict(args or {})
    out: dict[str, Any] = {}
    for k, v in sorted(a.items()):
        if v is None or v == "":
            continue
        if isinstance(v, str):
            s = v.strip()
            if k in {
                "destination",
                "hotel_city",
                "city",
                "location",
                "origin",
                "arrive_airport",
                "return_airport",
            }:
                s = s.lower().replace("+", " ")
            out[k] = s
        else:
            out[k] = v
    out["_tool"] = name
    return out


def fixture_lookup_key(name: str, args: dict[str, Any] | None, destination: str = "") -> str:
    """Coarse key: tool + destination (fixtures are per-destination scrapes)."""
    dest = (destination or "").strip().lower()
    if not dest:
        a = args or {}
        dest = str(
            a.get("destination")
            or a.get("hotel_city")
            or a.get("city")
            or a.get("location")
            or ""
        ).strip().lower()
    return f"{name}|{dest}"


def load_fixtures(path: Path | None = None) -> list[dict[str, Any]]:
    ensure_data_dirs()
    return load_jsonl(path or TOOL_FIXTURES_PATH)


def fixtures_by_destination(rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    rows = rows if rows is not None else load_fixtures()
    by: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.get("ok", True):
            continue
        dest = str(row.get("destination") or "").strip().lower()
        if dest:
            by[dest] = row
    return by


def perturb_text(
    text: str,
    *,
    price_jitter: float = 0.08,
    rng: random.Random | None = None,
) -> str:
    """Light price jitter so the student does not memorize exact listings."""
    rng = rng or random.Random()
    if not text:
        return text

    def _price(m: re.Match[str]) -> str:
        prefix, num = m.group(1), m.group(2)
        try:
            val = float(num.replace(",", ""))
        except ValueError:
            return m.group(0)
        factor = 1.0 + rng.uniform(-price_jitter, price_jitter)
        new = max(1.0, val * factor)
        if new >= 100:
            return f"{prefix}{new:,.0f}"
        return f"{prefix}{new:,.2f}"

    return _HKD_RE.sub(_price, text)


def shuffle_numbered_blocks(text: str, rng: random.Random | None = None) -> str:
    """Shuffle consecutive 'N. ...' option lines when present."""
    rng = rng or random.Random()
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"^\s*\d+\.\s+", line):
            if current and not re.match(r"^\s*\d+\.\s+", current[0]):
                blocks.append(current)
                current = [line]
            else:
                current.append(line)
        else:
            if current and re.match(r"^\s*\d+\.\s+", current[0]):
                blocks.append(current)
                current = [line]
            else:
                current.append(line)
    if current:
        blocks.append(current)

    out_lines: list[str] = []
    for block in blocks:
        if block and re.match(r"^\s*\d+\.\s+", block[0]) and len(block) > 1:
            numbered = list(block)
            rng.shuffle(numbered)
            # Re-number
            renum: list[str] = []
            for i, line in enumerate(numbered):
                renum.append(re.sub(r"^\s*\d+\.", f"{i}.", line, count=1))
            out_lines.extend(renum)
        else:
            out_lines.extend(block)
    return "\n".join(out_lines)


class FixtureReplayer:
    """Serve cached tool outputs; monkeypatch-friendly."""

    def __init__(
        self,
        fixtures: dict[str, dict[str, Any]] | None = None,
        *,
        perturb: bool = True,
        seed: int = 0,
    ) -> None:
        self.by_dest = fixtures if fixtures is not None else fixtures_by_destination()
        self.perturb = perturb
        self.rng = random.Random(seed)
        self.hits = 0
        self.misses = 0

    def get_destination_blob(self, destination: str) -> dict[str, Any] | None:
        return self.by_dest.get(destination.strip().lower())

    def tool_result(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        destination_hint: str = "",
    ) -> str:
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                args = {}
        else:
            args = dict(arguments or {})

        dest = destination_hint or str(
            args.get("destination")
            or args.get("hotel_city")
            or args.get("city")
            or args.get("location")
            or ""
        )
        row = self.get_destination_blob(dest) if dest else None
        # Fallback: any fixture if single-city tools without dest
        if row is None and self.by_dest:
            # Try matching airport-ish destination against fixture destinations
            low = dest.lower()
            for k, v in self.by_dest.items():
                if low and (low in k or k in low):
                    row = v
                    break
        if row is None:
            self.misses += 1
            return (
                f"[fixture miss] No cached scrape for tool={name!r} destination={dest!r}. "
                "Run: python -m distill.record_fixtures --resume"
            )

        tools = row.get("tools") or {}
        text = ""
        if name in tools and isinstance(tools[name], str):
            text = tools[name]
        elif name == "propose_trip_route" and row.get("route_text"):
            text = str(row["route_text"])
        elif name in {"plan_trip", "search_flights", "search_hotels"} and row.get("plan_text"):
            text = str(row.get("plan_text") or "")
        elif name == "search_attractions" and row.get("attractions_text"):
            text = str(row["attractions_text"])
        elif name == "search_cars" and row.get("cars_text"):
            text = str(row["cars_text"])
        else:
            # Prefer plan_text as a rich fallback for scrape tools
            text = str(
                tools.get(name)
                or row.get("plan_text")
                or row.get("route_text")
                or ""
            )

        if not text:
            self.misses += 1
            return f"[fixture miss] Empty cache for tool={name!r} destination={dest!r}."

        self.hits += 1
        if self.perturb:
            text = perturb_text(text, rng=self.rng)
            if name in {"search_flights", "compare_flight_prices", "search_hotels", "search_cars"}:
                text = shuffle_numbered_blocks(text, rng=self.rng)
        return text


def install_dispatch_patch(
    replayer: FixtureReplayer,
    *,
    destination_hint_fn: Callable[[], str] | None = None,
) -> Callable[[], None]:
    """Monkeypatch travel_agent.browser_tools.dispatch_tool and agent.dispatch_tool usage.

    Returns an uninstall function.
    """
    import travel_agent.browser_tools as bt
    import travel_agent.agent as agent_mod

    original = bt.dispatch_tool

    def patched(browser: Any, name: str, arguments: dict[str, Any] | str) -> str:
        hint = destination_hint_fn() if destination_hint_fn else ""
        # Prefer last proposed route destination on browser
        if not hint and getattr(browser, "selection_context", None):
            hint = str(getattr(browser.selection_context, "destination", "") or "")
        if not hint:
            route = getattr(browser, "last_proposed_route", None)
            if route is not None:
                stays = getattr(route, "stays", None) or []
                if stays:
                    hint = str(getattr(stays[0], "city", "") or "")
        return replayer.tool_result(name, arguments, destination_hint=hint)

    bt.dispatch_tool = patched  # type: ignore[assignment]
    # agent imports dispatch_tool by name — patch module attribute used at call time
    if hasattr(agent_mod, "dispatch_tool"):
        agent_mod.dispatch_tool = patched  # type: ignore[attr-defined]

    def uninstall() -> None:
        bt.dispatch_tool = original  # type: ignore[assignment]
        if hasattr(agent_mod, "dispatch_tool"):
            agent_mod.dispatch_tool = original  # type: ignore[attr-defined]

    return uninstall
