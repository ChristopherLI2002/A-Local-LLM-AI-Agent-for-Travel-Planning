"""Phase 3a: score / filter teacher rollouts with project validators."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.paths import RAW_ROLLOUTS_PATH, SCORED_PATH, ensure_data_dirs
from distill.session import load_jsonl, append_jsonl

_VAGUE_RE = re.compile(
    r"(?i)\b(attractions?\s*&\s*tours|local dinner(?:\s+and\s+unwind)?|sightseeing)\b"
)
_HKD_RE = re.compile(r"HK\$?\s*([0-9][0-9,]*(?:\.\d+)?)", re.I)
_DAY_RE = re.compile(r"(?im)^Day\s+(\d+)\s*:")


def _has_vague_activity_language(final: str) -> bool:
    """True for vague activity labels; ignore rule-echo lines and 'sightseeing at X'."""
    for m in _VAGUE_RE.finditer(final or ""):
        line_start = final.rfind("\n", 0, m.start()) + 1
        line_end = final.find("\n", m.end())
        line = final[line_start : line_end if line_end != -1 else None]
        if re.search(
            r"(?i)\b(no|never|not|don't|do not)\b.{0,60}\b(attractions|local dinner|sightseeing)\b",
            line,
        ):
            continue
        if re.search(r"(?i)\bsightseeing\s+at\b", line):
            continue
        return True
    return False


def _tool_corpus(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.get("role") == "tool":
            parts.append(str(m.get("content") or ""))
    return "\n".join(parts)


def _final_assistant(messages: list[dict[str, Any]], fallback: str = "") -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            content = str(m.get("content") or "").strip()
            if content:
                return content
    return fallback or ""


def _pick_final_text(row: dict[str, Any]) -> str:
    """Prefer explicit final_answer (post-processed) over earlier assistant echoes."""
    explicit = str(row.get("final_answer") or "").strip()
    from_msgs = _final_assistant(row.get("messages") or [], "")
    if explicit and from_msgs:
        # Agent finalize may leave a short echo in history before tools.
        if len(explicit) >= len(from_msgs) or (
            re.search(r"(?im)^Day\s+1\s*:", explicit)
            and not re.search(r"(?im)^Day\s+1\s*:", from_msgs)
        ):
            return explicit
        return from_msgs
    return explicit or from_msgs


def score_rollout(row: dict[str, Any]) -> dict[str, Any]:
    """Return scoring dict with pass/fail and reasons."""
    from travel_agent.itinerary_parse import parse_itinerary
    from travel_agent.trip_urls import (
        extract_booking_urls,
        is_trusted_hotel_detail_url,
        score_booking_url,
    )

    messages = row.get("messages") or []
    final = _pick_final_text(row)
    tool_text = _tool_corpus(messages)
    nights = int((row.get("prompt") or {}).get("nights") or 0)
    fails: list[str] = []
    warns: list[str] = []

    if not row.get("ok"):
        fails.append("rollout_not_ok")
    if len(final) < 200:
        fails.append("final_too_short")

    parsed = parse_itinerary(final)
    if not parsed.flight and "recommended flight" not in final.lower():
        fails.append("missing_flight_section")
    if not parsed.hotel and "recommended hotel" not in final.lower():
        fails.append("missing_hotel_section")
    # Prefer parser (accepts "Day 1:", "### Day 1 — …", etc.); fall back to strict regex.
    day_nums = []
    for d in parsed.days or []:
        m = re.search(r"(?i)day\s+(\d+)", str(getattr(d, "title", "") or ""))
        if m:
            day_nums.append(int(m.group(1)))
    if not day_nums:
        day_nums = [int(m.group(1)) for m in _DAY_RE.finditer(final)]
    if nights > 0:
        if len(day_nums) < nights:
            fails.append(f"day_blocks={len(day_nums)}<{nights}")
        elif day_nums and max(day_nums) < nights:
            fails.append(f"max_day={max(day_nums)}<{nights}")

    if _has_vague_activity_language(final):
        fails.append("vague_activity_language")

    urls = extract_booking_urls(final)
    # Invented URL: hk.trip.com link in final not present in tool corpus
    final_links = re.findall(r"https://hk\.trip\.com[^\s\)\]\"']+", final, flags=re.I)
    for link in final_links:
        if tool_text and link.split("?")[0].lower() not in tool_text.lower():
            # Allow if hotelId appears in tools
            hid = re.search(r"hotelId=(\d+)", link, re.I)
            if hid and hid.group(1) in tool_text:
                continue
            if "hk.trip.com" in tool_text.lower():
                warns.append(f"url_not_in_tools:{link[:80]}")
            else:
                fails.append("invented_or_unmatched_url")
                break

    hotel_url = urls.get("hotel") or ""
    if hotel_url:
        if "/hotels/list" in hotel_url.lower() and "hotelId=" not in hotel_url.lower():
            # Prefer fail when tools had a detail URL
            if "hotels/detail" in tool_text.lower() or "hotelId=" in tool_text:
                fails.append("hotel_list_url_when_detail_exists")
        elif not is_trusted_hotel_detail_url(hotel_url):
            if score_booking_url(hotel_url, "hotel") < 50:
                warns.append("weak_hotel_url")

    # Prices in final should appear in tools (approximate)
    final_prices = {m.group(1).replace(",", "") for m in _HKD_RE.finditer(final)}
    if tool_text and final_prices:
        missing = 0
        for p in list(final_prices)[:8]:
            if p not in tool_text.replace(",", ""):
                missing += 1
        if missing >= max(2, len(final_prices) // 2):
            warns.append("many_prices_not_in_tools")

    # Tool-call structure
    n_tool = int(row.get("n_tool_rounds") or 0)
    if n_tool < 1 and row.get("prompt", {}).get("kind") == "plan":
        fails.append("no_tool_calls")

    first_tool = ""
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tc = m["tool_calls"][0]
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            first_tool = str(fn.get("name") or "")
            break
    if row.get("prompt", {}).get("kind") == "plan" and first_tool and first_tool != "propose_trip_route":
        warns.append(f"first_tool={first_tool}")

    score = 100.0
    score -= 25.0 * len(fails)
    score -= 5.0 * len(warns)
    score = max(0.0, score)
    passed = len(fails) == 0

    return {
        "pass": passed,
        "score": score,
        "fails": fails,
        "warns": warns,
        "n_days_found": len(day_nums),
        "nights": nights,
        "first_tool": first_tool,
        "hotel_url": hotel_url[:200],
        "flight_url": (urls.get("flight") or "")[:200],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score teacher rollouts")
    parser.add_argument("--input", type=Path, default=RAW_ROLLOUTS_PATH)
    parser.add_argument("--output", type=Path, default=SCORED_PATH)
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--best-of-frac", type=float, default=0.30, help="Unused here; for rollout sampling")
    parser.add_argument("--rewrite", action="store_true", help="Overwrite output")
    args = parser.parse_args(argv)

    ensure_data_dirs()
    rows = load_jsonl(args.input)
    if not rows:
        print(f"No rollouts at {args.input}")
        return 1

    if args.rewrite and args.output.exists():
        args.output.unlink()

    # Group by key for best-of-n
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(str(row.get("key")), []).append(row)

    kept = 0
    rejected = 0
    for key, group in by_key.items():
        scored_group: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for row in group:
            sc = score_rollout(row)
            scored_group.append((float(sc["score"]), sc, row))
        scored_group.sort(key=lambda x: x[0], reverse=True)
        best_score, best_sc, best_row = scored_group[0]
        out = {
            "key": key,
            "ok": bool(best_sc["pass"]) and best_score >= args.min_score,
            "score_detail": best_sc,
            "n_candidates": len(group),
            "rollout": {
                "key": best_row.get("key"),
                "destination": best_row.get("destination"),
                "prompt": best_row.get("prompt"),
                "messages": best_row.get("messages"),
                "final_answer": best_row.get("final_answer"),
                "teacher": best_row.get("teacher"),
                "llm_calls": best_row.get("llm_calls"),
                "n_tool_rounds": best_row.get("n_tool_rounds"),
                "booking_links": best_row.get("booking_links"),
            },
        }
        append_jsonl(args.output, out)
        if out["ok"]:
            kept += 1
        else:
            rejected += 1

    print(f"Scored {len(by_key)} keys → kept={kept} rejected={rejected}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
