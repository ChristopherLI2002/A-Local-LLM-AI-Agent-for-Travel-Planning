"""Phase 3b: build Qwen2.5 ChatML train/val JSONL with compact prompts."""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.compact_prompt import (
    COMPACT_SYSTEM_PROMPT,
    SLIM_TOOL_DEFINITIONS,
    compact_tool_result,
)
from distill.paths import SCORED_PATH, TRAIN_PATH, VAL_PATH, ensure_data_dirs
from distill.session import load_jsonl, append_jsonl
from travel_agent.agent import SYSTEM_PROMPT
from travel_agent.browser_tools import TOOL_DEFINITIONS


def _rewrite_system(messages: list[dict[str, Any]], *, compact: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sys_content = COMPACT_SYSTEM_PROMPT if compact else SYSTEM_PROMPT
    replaced = False
    for m in messages:
        if m.get("role") == "system" and not replaced:
            out.append({"role": "system", "content": sys_content})
            replaced = True
        elif compact and m.get("role") == "tool":
            out.append(
                {
                    **dict(m),
                    "content": compact_tool_result(
                        str(m.get("content") or ""),
                        str(m.get("tool_name") or ""),
                    ),
                }
            )
        else:
            out.append(dict(m))
    if not replaced:
        out.insert(0, {"role": "system", "content": sys_content})
    return out


def _tool_round_count(messages: list[dict[str, Any]]) -> int:
    return sum(
        1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    )


def _sample_tool_name(sample: dict[str, Any]) -> str:
    for m in reversed(sample.get("messages") or []):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        tc = m["tool_calls"][0]
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        return str(fn.get("name") or "")
    return ""


def _ideal_tool_sequence(messages: list[dict[str, Any]]) -> bool:
    names: list[str] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                names.append(str(fn.get("name") or ""))
    if not names:
        return False
    return names[0] == "propose_trip_route" and "plan_trip" in names


def _split_into_sft_samples(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Emit one SFT sample per assistant turn (tool call or final)."""
    samples: list[dict[str, Any]] = []
    # Walk prefixes ending at each assistant message
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        prefix = messages[: i + 1]
        samples.append(
            {
                "messages": prefix,
                "tools": tools,
                "meta": {
                    **meta,
                    "turn_index": i,
                    "has_tool_calls": bool(m.get("tool_calls")),
                },
            }
        )
    return samples


def build_samples(
    scored_rows: list[dict[str, Any]],
    *,
    compact: bool = True,
    oversample_tools: bool = True,
    oversample_finals: int = 0,
    oversample_clean_tools: int = 4,
    max_clean_tool_rounds: int = 4,
    drop_messy_tool_turns: bool = True,
    messy_tool_rounds: int = 5,
) -> list[dict[str, Any]]:
    tools = SLIM_TOOL_DEFINITIONS if compact else TOOL_DEFINITIONS
    samples: list[dict[str, Any]] = []
    for row in scored_rows:
        if not row.get("ok"):
            continue
        rollout = row.get("rollout") or {}
        messages = _rewrite_system(list(rollout.get("messages") or []), compact=compact)
        if len(messages) < 2:
            continue
        meta = {
            "key": row.get("key"),
            "destination": rollout.get("destination")
            or (rollout.get("prompt") or {}).get("destination"),
            "score": (row.get("score_detail") or {}).get("score"),
            "teacher": rollout.get("teacher"),
        }
        rounds = _tool_round_count(messages)
        clean = rounds <= max_clean_tool_rounds
        ideal = clean and _ideal_tool_sequence(messages)
        messy = rounds > messy_tool_rounds

        turn_samples = _split_into_sft_samples(messages, tools=tools, meta=meta)
        for s in turn_samples:
            has_tools = bool(s["meta"].get("has_tool_calls"))
            if drop_messy_tool_turns and messy and has_tools:
                continue
            samples.append(s)
            if oversample_tools and has_tools:
                samples.append(s)
            if has_tools and clean and oversample_clean_tools > 1:
                tool_name = _sample_tool_name(s)
                if tool_name in {"propose_trip_route", "plan_trip"}:
                    extra = oversample_clean_tools + (2 if ideal else 0)
                    for _ in range(extra - 1):
                        samples.append(s)
            if oversample_finals > 0 and not has_tools:
                content = ""
                for m in reversed(s.get("messages") or []):
                    if m.get("role") == "assistant" and m.get("content"):
                        content = str(m["content"])
                        break
                weight = oversample_finals
                if "Day 1:" in content or "Day 1 :" in content:
                    weight = max(weight, oversample_finals)
                else:
                    weight = max(1, oversample_finals // 2)
                for _ in range(weight - 1):
                    samples.append(s)
    return samples


def split_by_destination(
    samples: list[dict[str, Any]],
    *,
    val_frac: float = 0.15,
    seed: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_dest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        dest = str((s.get("meta") or {}).get("destination") or "unknown").lower()
        by_dest[dest].append(s)
    dests = sorted(by_dest.keys())
    rng = random.Random(seed)
    rng.shuffle(dests)
    n_val = max(1, int(round(len(dests) * val_frac))) if dests else 0
    val_dests = set(dests[:n_val])
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    for d, group in by_dest.items():
        if d in val_dests:
            val.extend(group)
        else:
            train.extend(group)
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ChatML train/val JSONL")
    parser.add_argument("--input", type=Path, default=SCORED_PATH)
    parser.add_argument("--train", type=Path, default=TRAIN_PATH)
    parser.add_argument("--val", type=Path, default=VAL_PATH)
    parser.add_argument("--val-split-by", choices=("destination",), default="destination")
    parser.add_argument("--val-frac", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--full-prompt", action="store_true", help="Keep full teacher prompt")
    parser.add_argument(
        "--oversample-finals",
        type=int,
        default=3,
        help="Extra copies of final-answer turns (Day itinerary). 0 disables.",
    )
    parser.add_argument(
        "--no-oversample-tools",
        action="store_true",
        help="Do not duplicate tool-call turns",
    )
    parser.add_argument(
        "--oversample-clean-tools",
        type=int,
        default=4,
        help="Extra copies of propose_trip_route/plan_trip turns from clean rollouts",
    )
    parser.add_argument(
        "--max-clean-tool-rounds",
        type=int,
        default=4,
        help="Rollouts with <= this many tool rounds count as clean",
    )
    parser.add_argument(
        "--messy-tool-rounds",
        type=int,
        default=5,
        help="Rollouts with more tool rounds drop intermediate tool turns",
    )
    parser.add_argument(
        "--keep-messy-tool-turns",
        action="store_true",
        help="Keep tool turns from long / thrashing rollouts",
    )
    args = parser.parse_args(argv)

    ensure_data_dirs()
    scored = load_jsonl(args.input)
    if not scored:
        print(f"No scored rollouts at {args.input}. Run: python -m distill.score")
        return 1

    samples = build_samples(
        scored,
        compact=not args.full_prompt,
        oversample_tools=not args.no_oversample_tools,
        oversample_finals=max(0, int(args.oversample_finals)),
        oversample_clean_tools=max(1, int(args.oversample_clean_tools)),
        max_clean_tool_rounds=max(1, int(args.max_clean_tool_rounds)),
        drop_messy_tool_turns=not args.keep_messy_tool_turns,
        messy_tool_rounds=max(1, int(args.messy_tool_rounds)),
    )
    if not samples:
        print("No passed samples to write.")
        return 1

    train, val = split_by_destination(samples, val_frac=args.val_frac, seed=args.seed)

    for path in (args.train, args.val):
        if path.exists():
            path.unlink()

    for s in train:
        append_jsonl(args.train, s)
    for s in val:
        append_jsonl(args.val, s)

    print(f"Wrote train={len(train)} → {args.train}")
    print(f"Wrote val={len(val)} → {args.val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
