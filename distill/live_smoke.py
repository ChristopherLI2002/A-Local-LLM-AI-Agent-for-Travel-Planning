"""Short live Playwright smoke (not fixture replay) for the distilled student.

Runs TravelAgent end-to-end with real Trip.com scrapes, then scores the final
answer with distill.score.score_rollout.

Usage:
  python -m distill.live_smoke
  python -m distill.live_smoke --limit 2
  python -m distill.live_smoke --show-browser
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Shared Playwright cache (same as launch.py / GUI)
_local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(_local, "ms-playwright"))

from distill.paths import ensure_data_dirs
from distill.score import score_rollout
from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.llm_select import SelectionContext
from travel_agent.planner_query import build_plan_query

OUT_DIR = Path(os.environ.get("DISTILL_OUT", r"D:\distill-out")) / "live_smoke"
RESULTS_PATH = OUT_DIR / "results.jsonl"

# Compact cases: short stays, future dates, no car (faster scrape path).
CASES = [
    {
        "id": "tokyo-4n",
        "destination": "Tokyo",
        "depart_date": "2026-10-12",
        "return_date": "2026-10-16",
        "nights": 4,
        "budget_hkd": 12000,
        "travel_styles": ["Food", "Culture"],
    },
    {
        "id": "seoul-3n",
        "destination": "Seoul",
        "depart_date": "2026-10-20",
        "return_date": "2026-10-23",
        "nights": 3,
        "budget_hkd": 9000,
        "travel_styles": ["Food", "Shopping"],
    },
    {
        "id": "singapore-3n",
        "destination": "Singapore",
        "depart_date": "2026-11-05",
        "return_date": "2026-11-08",
        "nights": 3,
        "budget_hkd": 10000,
        "travel_styles": ["First-time", "Food"],
    },
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(row: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_case(case: dict) -> dict:
    query = build_plan_query(
        destination=case["destination"],
        depart_date=case["depart_date"],
        return_date=case["return_date"],
        budget_hkd=float(case["budget_hkd"]),
        origin="Hong Kong",
        rent_car=False,
        travel_styles=list(case.get("travel_styles") or []),
        nights=int(case["nights"]),
    )
    print(f"\n=== LIVE {case['id']} ===", flush=True)
    print(f"model={settings.ollama_model} student_mode={settings.student_mode}", flush=True)
    print(query[:200].replace("\n", " ") + "...", flush=True)

    agent = TravelAgent(student_mode=True)
    agent.force_rent_car = False
    agent.browser.selection_context = SelectionContext(
        origin="Hong Kong",
        destination=case["destination"],
        nights=int(case["nights"]),
        budget_hkd=float(case["budget_hkd"]),
        travel_styles=", ".join(case.get("travel_styles") or []),
        checkin=case["depart_date"],
        checkout=case["return_date"],
        rent_car=False,
        adults=2,
    )

    def _on_tool_start(name: str, args: dict) -> None:
        keys = (
            "origin",
            "destination",
            "depart_date",
            "return_date",
            "hotel_city",
            "nights",
            "budget_hkd",
        )
        shown = {k: args.get(k) for k in keys if args.get(k) not in (None, "")}
        print(f"  → tool {name} {shown}", flush=True)

    def _on_tool_end(name: str, preview: str) -> None:
        low = (preview or "").lower()
        flag = "ok"
        if "bad arguments" in low or "failed:" in low:
            flag = "ERR"
        print(f"  ← tool {name} [{flag}] {len(preview or '')} chars", flush=True)

    agent.on_tool_start = _on_tool_start
    agent.on_tool_end = _on_tool_end
    agent.on_status = lambda msg: print(f"  … {msg}", flush=True)

    t0 = time.time()
    err = ""
    answer = ""
    try:
        print("  … starting browser", flush=True)
        agent.start()
        answer = agent.chat(query)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed = round(time.time() - t0, 1)

    n_tool_rounds = sum(
        1 for m in agent.messages if m.get("role") == "assistant" and m.get("tool_calls")
    )
    row = {
        "id": case["id"],
        "ok": not bool(err),
        "error": err,
        "elapsed_s": elapsed,
        "final_answer": answer,
        "messages": list(agent.messages),
        "n_tool_rounds": n_tool_rounds,
        "prompt": {
            "kind": "plan",
            "destination": case["destination"],
            "nights": case["nights"],
            "budget_hkd": case["budget_hkd"],
            "depart_date": case["depart_date"],
            "return_date": case["return_date"],
        },
        "model": settings.ollama_model,
        "ts": _utc(),
    }
    scored = score_rollout(row)
    row["score"] = {
        "passed": bool(scored.get("pass")),
        "fails": list(scored.get("fails") or []),
        "warns": list(scored.get("warns") or []),
    }
    # Keep results file lean — drop full message dump after scoring
    slim = {k: v for k, v in row.items() if k != "messages"}
    slim["final_preview"] = (answer or "")[:800]
    slim["answer_chars"] = len(answer or "")
    _append(slim)

    status = "PASS" if row["score"]["passed"] else "FAIL"
    print(
        f"[{status}] {case['id']}  {elapsed}s  chars={len(answer or '')}  "
        f"fails={row['score']['fails']}",
        flush=True,
    )
    try:
        agent.close()
    except Exception:
        try:
            agent.browser.close()
        except Exception:
            pass
    return slim


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Playwright smoke for student agent")
    parser.add_argument("--limit", type=int, default=3, help="Max cases (default 3)")
    parser.add_argument("--ids", type=str, default="", help="Comma-separated case ids")
    parser.add_argument("--show-browser", action="store_true", help="Disable headless")
    args = parser.parse_args()

    if args.show_browser:
        os.environ["HEADLESS"] = "false"
        # settings already loaded — force via env for TripBrowser
        settings.headless = False  # type: ignore[attr-defined]

    ensure_data_dirs()
    cases = CASES
    if args.ids.strip():
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in CASES if c["id"] in want]
    cases = cases[: max(1, int(args.limit))]

    print(
        f"Live smoke: {len(cases)} case(s) → {RESULTS_PATH}",
        flush=True,
    )
    results = [run_case(c) for c in cases]
    passed = sum(1 for r in results if r.get("score", {}).get("passed"))
    total = len(results)
    print(f"\n=== SUMMARY {passed}/{total} passed ===", flush=True)
    for r in results:
        print(
            f"  {r['id']}: {'PASS' if r['score']['passed'] else 'FAIL'} "
            f"{r['elapsed_s']}s fails={r['score']['fails']}",
            flush=True,
        )
    summary = {
        "ts": _utc(),
        "passed": passed,
        "total": total,
        "model": settings.ollama_model,
        "cases": [
            {
                "id": r["id"],
                "passed": r["score"]["passed"],
                "elapsed_s": r["elapsed_s"],
                "fails": r["score"]["fails"],
            }
            for r in results
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary_latest.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
