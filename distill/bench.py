"""Phase 6: benchmark student vs baseline on fixture-replayed held-out prompts (gated)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.compact_prompt import COMPACT_SYSTEM_PROMPT, SLIM_TOOL_DEFINITIONS, footprint
from distill.fixtures import FixtureReplayer, fixtures_by_destination, install_dispatch_patch
from distill.paths import BENCH_STATUS_PATH, VAL_PATH, out_dir, ensure_data_dirs
from distill.prompt_bank import expand_plan_prompts
from distill.score import score_rollout
from distill.session import SessionRunner, append_jsonl


def _run_model_on_prompt(
    model: str,
    query: str,
    *,
    student_mode: bool,
    prompt: Any | None = None,
) -> dict[str, Any]:
    from travel_agent.agent import TravelAgent
    from travel_agent.llm_select import SelectionContext

    agent = TravelAgent(model=model, student_mode=student_mode)
    if prompt is not None:
        agent.force_rent_car = bool(getattr(prompt, "rent_car", False))
        agent.browser.selection_context = SelectionContext(
            origin=getattr(prompt, "origin", "Hong Kong"),
            destination=getattr(prompt, "destination", ""),
            nights=int(getattr(prompt, "nights", 0) or 0),
            budget_hkd=float(getattr(prompt, "budget_hkd", 0) or 0),
            travel_styles=", ".join(getattr(prompt, "styles", ()) or ()),
            checkin=getattr(prompt, "depart", ""),
            checkout=getattr(prompt, "return_date", ""),
            rent_car=bool(getattr(prompt, "rent_car", False)),
        )

    t0 = time.monotonic()
    err = ""
    answer = ""
    try:
        answer = agent.chat(query)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.monotonic() - t0, 2)

    row = {
        "model": model,
        "student_mode": student_mode,
        "ok": bool(answer) and not err,
        "error": err,
        "elapsed_sec": elapsed,
        "final_answer": answer[:15_000],
        "messages": agent.messages,
        "n_tool_rounds": sum(
            1 for m in agent.messages if m.get("role") == "assistant" and m.get("tool_calls")
        ),
        "prefill_overhead_tokens": footprint()["total_overhead_tokens"]
        if student_mode
        else None,
    }
    try:
        agent.close()
    except Exception:
        pass
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark distilled students")
    parser.add_argument(
        "--models",
        default="qwen2.5:3b",
        help="Comma-separated Ollama tags",
    )
    parser.add_argument("--max-hours", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--student-tags",
        default="voyage-student-0-5b,voyage-student-1-5b",
        help="Tags that should use compact prompt",
    )
    args = parser.parse_args(argv)

    ensure_data_dirs()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    student_tags = {t.strip() for t in args.student_tags.split(",") if t.strip()}
    prompts = expand_plan_prompts(max_prompts=args.limit)
    by_dest = fixtures_by_destination()
    if not by_dest:
        print("No fixtures — bench needs: python -m distill.record_fixtures")
        return 2

    progress = out_dir() / "bench_progress.jsonl"
    replayer = FixtureReplayer(by_dest, perturb=False, seed=1)
    current = {"dest": ""}
    uninstall = install_dispatch_patch(replayer, destination_hint_fn=lambda: current["dest"])

    units: list[tuple[str, dict[str, Any]]] = []
    for m in models:
        for p in prompts:
            key = f"{m}|{p.key}"
            units.append((key, {"model": m, "prompt": p}))

    runner = SessionRunner(
        phase="bench",
        progress_path=progress,
        resume_cmd="python -m distill.bench --resume --max-hours 1",
        max_hours=args.max_hours,
        key_field="key",
        status_path=BENCH_STATUS_PATH,
    )

    def work(key: str, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload["model"]
        prompt = payload["prompt"]
        current["dest"] = prompt.destination
        student_mode = model in student_tags or "student" in model.lower()
        print(f">>> bench {model} / {prompt.destination}", flush=True)
        row = _run_model_on_prompt(
            model,
            prompt.query,
            student_mode=student_mode,
            prompt=prompt,
        )
        row["key"] = key
        row["destination"] = prompt.destination
        row["prompt_key"] = prompt.key
        row["nights"] = prompt.nights
        # Score
        sc = score_rollout(
            {
                "ok": row.get("ok"),
                "messages": row.get("messages"),
                "final_answer": row.get("final_answer"),
                "n_tool_rounds": row.get("n_tool_rounds"),
                "prompt": prompt.to_dict(),
            }
        )
        row["score_detail"] = sc
        row["ok"] = bool(row.get("ok")) and bool(sc.get("pass"))
        print(f"<<< {model} pass={sc.get('pass')} {row['elapsed_sec']}s", flush=True)
        return row

    try:
        runner.run(units, work, total_planned=len(units), default_unit_sec=180.0)
    finally:
        uninstall()

    # Summary
    from distill.session import load_jsonl

    rows = load_jsonl(progress)
    summary: dict[str, Any] = {}
    for r in rows:
        m = r.get("model") or "?"
        s = summary.setdefault(m, {"n": 0, "pass": 0, "sec": 0.0})
        s["n"] += 1
        s["pass"] += 1 if (r.get("score_detail") or {}).get("pass") else 0
        s["sec"] += float(r.get("elapsed_sec") or 0)
    for m, s in summary.items():
        s["pass_rate"] = round(s["pass"] / max(1, s["n"]), 3)
        s["avg_sec"] = round(s["sec"] / max(1, s["n"]), 1)
    outp = out_dir() / "bench_summary.json"
    outp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
