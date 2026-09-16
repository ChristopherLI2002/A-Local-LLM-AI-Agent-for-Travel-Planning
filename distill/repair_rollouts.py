"""Repair teacher finals that omit Day N: blocks (common with small teachers).

Deterministic fill from destination_guides / synthesize_day_blocks — no extra
LLM calls. Updates final_answer and the last non-tool assistant message so SFT
sees the repaired text. Backs up raw_rollouts.jsonl before rewrite.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.paths import RAW_ROLLOUTS_PATH, ensure_data_dirs
from distill.session import load_jsonl


def _final_of(row: dict[str, Any]) -> str:
    final = str(row.get("final_answer") or "")
    if final.strip():
        return final
    for m in reversed(row.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and not m.get("tool_calls"):
            return str(m.get("content") or "")
    return ""


def _needs_day_repair(text: str, nights: int) -> bool:
    from travel_agent.itinerary_parse import parse_itinerary

    parsed = parse_itinerary(text or "")
    n = max(0, int(nights or 0))
    if n <= 0:
        return len(parsed.days) == 0
    return len(parsed.days) < n


def _day_section(*, destination: str, nights: int, styles: list[str] | None) -> str:
    from travel_agent.itinerary_parse import synthesize_day_blocks

    n = max(1, int(nights or 1))
    blocks = synthesize_day_blocks(
        nights=n,
        destination=destination or "",
        styles=styles,
    )
    lines = ["", "Day-by-day itinerary"]
    for i, block in enumerate(blocks, start=1):
        # Exact "Day N:" lines required by distill.score and SYSTEM_PROMPT.
        title = (block.title or "").strip()
        # title is often "Day i - Theme"; keep theme after colon body header
        theme = title
        for prefix in (f"Day {i} - ", f"Day {i}: ", f"Day {i}—", f"Day {i} – "):
            if theme.startswith(prefix):
                theme = theme[len(prefix) :].strip()
                break
        header = f"Day {i}:" + (f" {theme}" if theme and not theme.lower().startswith("day ") else "")
        lines.append(header)
        body = (block.body or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _patch_messages(messages: list[Any], new_final: str) -> list[Any]:
    out = list(messages or [])
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if isinstance(m, dict) and m.get("role") == "assistant" and not m.get("tool_calls"):
            patched = dict(m)
            patched["content"] = new_final
            out[i] = patched
            return out
    out.append({"role": "assistant", "content": new_final})
    return out


def repair_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not row.get("ok"):
        return row, False
    prompt = row.get("prompt") or {}
    nights = int(prompt.get("nights") or 0)
    final = _final_of(row)
    if not _needs_day_repair(final, nights):
        return row, False
    styles = prompt.get("styles") or []
    if isinstance(styles, str):
        styles = [styles]
    dest = str(row.get("destination") or prompt.get("destination") or "")
    section = _day_section(destination=dest, nights=nights or 1, styles=list(styles))
    # Avoid duplicating a bare "Day-by-day" heading if already present without days
    base = final.rstrip()
    if "day-by-day itinerary" in base.lower():
        new_final = base + "\n" + section.replace("Day-by-day itinerary\n", "", 1)
    else:
        new_final = base + "\n" + section
    patched = dict(row)
    patched["final_answer"] = new_final[:20_000]
    patched["messages"] = _patch_messages(list(row.get("messages") or []), new_final[:20_000])
    patched["day_blocks_repaired"] = True
    return patched, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject Day N: blocks into teacher rollouts")
    parser.add_argument("--input", type=Path, default=RAW_ROLLOUTS_PATH)
    parser.add_argument("--output", type=Path, default=None, help="Default: overwrite input")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ensure_data_dirs()
    rows = load_jsonl(args.input)
    if not rows:
        print(f"No rollouts at {args.input}")
        return 1

    out_path = args.output or args.input
    repaired = 0
    skipped = 0
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        new_row, did = repair_row(row)
        out_rows.append(new_row)
        if did:
            repaired += 1
        else:
            skipped += 1

    print(f"Repaired {repaired} rollouts; left unchanged {skipped}")
    if args.dry_run:
        print("Dry-run — not writing")
        return 0

    if out_path.resolve() == args.input.resolve():
        bak = args.input.with_suffix(args.input.suffix + ".bak_pre_day_repair")
        if not bak.exists():
            bak.write_text(args.input.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Backup → {bak}")

    with out_path.open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
