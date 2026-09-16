"""Phase 2: teacher rollouts with fixture-replayed tools (no live scrape)."""

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

from distill.fixtures import FixtureReplayer, fixtures_by_destination, install_dispatch_patch
from distill.paths import RAW_ROLLOUTS_PATH, TEACHER_STATUS_PATH, ensure_data_dirs
from distill.prompt_bank import DistillPrompt, all_prompts
from distill.session import SessionRunner


def _to_plain(obj: Any) -> Any:
    """Convert ollama Message / pydantic objects into JSON-safe dicts/lists."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items() if k != "images"}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(x) for x in obj]
    if hasattr(obj, "model_dump"):
        return _to_plain(obj.model_dump())
    if hasattr(obj, "dict") and callable(obj.dict):
        try:
            return _to_plain(obj.dict())
        except Exception:
            pass
    # ollama.Message and similar attribute bags
    if hasattr(obj, "role") or hasattr(obj, "content") or hasattr(obj, "tool_calls"):
        item: dict[str, Any] = {}
        for key in ("role", "content", "tool_calls", "tool_name", "name"):
            if hasattr(obj, key):
                val = getattr(obj, key)
                if val is not None:
                    item[key] = _to_plain(val)
        return item
    return str(obj)


def _serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item = _to_plain(m)
        if not isinstance(item, dict):
            item = {"role": "assistant", "content": str(item)}
        if item.get("tool_calls") is not None:
            item["tool_calls"] = json.loads(
                json.dumps(item["tool_calls"], default=str)
            )
        out.append(item)
    return out


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """Windows background consoles can raise OSError 22 on flush."""
    kwargs.setdefault("flush", True)
    try:
        print(*args, **kwargs)
    except OSError:
        try:
            kwargs.pop("flush", None)
            print(*args, **kwargs)
        except OSError:
            pass


def _run_one_rollout(
    prompt: DistillPrompt,
    *,
    teacher: str,
    tool_turn_teacher: str | None,
    host: str,
    destination_hint: str,
) -> dict[str, Any]:
    from travel_agent.agent import TravelAgent
    from travel_agent.config import settings
    from travel_agent.llm_select import SelectionContext

    settings.ollama_model = teacher
    agent = TravelAgent(model=teacher)
    agent.force_rent_car = bool(prompt.rent_car)
    agent.browser.selection_context = SelectionContext(
        origin=prompt.origin,
        destination=prompt.destination,
        nights=prompt.nights,
        budget_hkd=prompt.budget_hkd,
        travel_styles=", ".join(prompt.styles),
        checkin=prompt.depart,
        checkout=prompt.return_date,
        rent_car=prompt.rent_car,
    )
    # Don't start a real browser — fixtures serve tools; start() opens Chromium.
    # TravelAgent.chat still calls tools via dispatch_tool which we patch.
    agent.browser.llm_model = teacher
    agent.browser.llm_host = host

    llm_calls: list[dict[str, Any]] = []
    tool_teacher = (tool_turn_teacher or "").strip() or None
    original_chat = agent._chat_with_recovery

    def wrapped_chat(*, tools: list[dict[str, Any]]) -> dict[str, Any]:
        # Prefer fast tool teacher while we expect tool calls; upgrade final turn
        use_model = teacher
        if tool_teacher and tool_teacher != teacher:
            agent.model = tool_teacher
            agent.browser.llm_model = tool_teacher
            resp = original_chat(tools=tools)
            msg = _to_plain(resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None))
            if not isinstance(msg, dict):
                msg = {}
            if msg.get("tool_calls"):
                llm_calls.append(
                    {
                        "model": tool_teacher,
                        "response": msg,
                        "kind": "tool_turn",
                    }
                )
                agent.model = teacher
                agent.browser.llm_model = teacher
                return resp
            agent.model = teacher
            agent.browser.llm_model = teacher
            resp2 = original_chat(tools=tools)
            msg2 = _to_plain(
                resp2.get("message") if isinstance(resp2, dict) else getattr(resp2, "message", None)
            )
            if not isinstance(msg2, dict):
                msg2 = {}
            llm_calls.append(
                {
                    "model": teacher,
                    "response": msg2,
                    "kind": "itinerary_turn",
                    "superseded_tool_teacher": True,
                }
            )
            return resp2

        agent.model = use_model
        agent.browser.llm_model = use_model
        resp = original_chat(tools=tools)
        msg = _to_plain(resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None))
        if not isinstance(msg, dict):
            msg = {}
        kind = "tool_turn" if msg.get("tool_calls") else "itinerary_turn"
        llm_calls.append(
            {
                "model": use_model,
                "response": msg,
                "kind": kind,
            }
        )
        return resp

    agent._chat_with_recovery = wrapped_chat  # type: ignore[method-assign]

    t0 = time.monotonic()
    answer = ""
    ok = False
    err = ""
    messages: list[dict[str, Any]] = []
    try:
        answer = agent.chat(prompt.query)
        ok = bool(answer) and not answer.startswith("(No response")
        messages = _serialize_messages(agent.messages)
    except Exception as exc:
        ok = False
        err = f"{type(exc).__name__}: {exc}"
        try:
            messages = _serialize_messages(agent.messages)
        except Exception:
            messages = []

    elapsed = round(time.monotonic() - t0, 2)
    row: dict[str, Any] = {
        "key": prompt.key,
        "ok": ok,
        "error": err,
        "elapsed_sec": elapsed,
        "prompt": prompt.to_dict(),
        "destination": prompt.destination,
        "teacher": teacher,
        "tool_turn_teacher": tool_teacher or teacher,
        "final_answer": (answer or "")[:20_000],
        "messages": messages,
        "llm_calls": llm_calls,
        "n_tool_rounds": sum(1 for c in llm_calls if c.get("kind") == "tool_turn"),
        "booking_links": dict(agent.booking_links or {}),
        "destination_hint": destination_hint,
    }
    try:
        agent.close()
    except Exception:
        pass
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teacher rollouts with fixture replay")
    parser.add_argument("--max-hours", type=float, default=1.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--target-rollouts", type=int, default=300)
    parser.add_argument(
        "--teacher",
        default="qwen2.5:7b",
        help="Main teacher (itinerary turns)",
    )
    parser.add_argument(
        "--tool-turn-teacher",
        default="",
        help="Optional faster model for tool-call turns (empty = use --teacher)",
    )
    parser.add_argument(
        "--fallback-teacher",
        default="",
        help="If primary teacher fails, retry this model once (empty to disable)",
    )
    parser.add_argument("--max-plan", type=int, default=400)
    parser.add_argument("--max-refine", type=int, default=80)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--require-fixtures",
        action="store_true",
        default=True,
        help="Abort if no fixtures (default True)",
    )
    parser.add_argument("--allow-empty-fixtures", action="store_true")
    args = parser.parse_args(argv)

    ensure_data_dirs()
    prompts = all_prompts(max_plan=args.max_plan, max_refine=args.max_refine)
    # Prefer plan prompts first, cap at target
    plans = [p for p in prompts if p.kind == "plan"]
    refines = [p for p in prompts if p.kind == "refine"]
    selected: list[DistillPrompt] = plans[: args.target_rollouts]
    # Fill with refine if under target
    if len(selected) < args.target_rollouts:
        selected.extend(refines[: args.target_rollouts - len(selected)])

    by_dest = fixtures_by_destination()
    _safe_print(f"Fixtures loaded for {len(by_dest)} destinations")
    _safe_print(f"Rollouts planned this target: {len(selected)}")
    _safe_print(f"Progress: {RAW_ROLLOUTS_PATH}")

    if not by_dest and not args.allow_empty_fixtures:
        _safe_print(
            "ERROR: no tool fixtures. Run:\n"
            "  python -m distill.record_fixtures --max-hours 1\n"
            "Or pass --allow-empty-fixtures for a dry wiring test.",
        )
        return 2

    if args.dry_run:
        for p in selected[:20]:
            _safe_print(f"  {p.key} {p.kind} {p.destination} nights={p.nights}")
        if len(selected) > 20:
            _safe_print(f"  ... +{len(selected) - 20} more")
        return 0

    from travel_agent.config import settings

    host = settings.ollama_host
    replayer = FixtureReplayer(by_dest, perturb=True, seed=args.seed)
    current_dest = {"value": ""}

    def hint_fn() -> str:
        return current_dest["value"]

    uninstall = install_dispatch_patch(replayer, destination_hint_fn=hint_fn)

    runner = SessionRunner(
        phase="teacher_rollout",
        progress_path=RAW_ROLLOUTS_PATH,
        resume_cmd="python -m distill.teacher_rollout --resume --max-hours 1",
        max_hours=args.max_hours,
        key_field="key",
        status_path=TEACHER_STATUS_PATH,
    )

    tool_teacher = args.tool_turn_teacher.strip() or None
    fallback_teacher = (args.fallback_teacher or "").strip()
    if fallback_teacher and fallback_teacher == args.teacher:
        fallback_teacher = ""

    def _retriable_teacher_error(err: str) -> bool:
        low = (err or "").lower()
        needles = (
            "timeout",
            "timed out",
            "cuda",
            "llama-server",
            "terminated",
            "connection",
            "10054",
            "502",
            "no ollama models",
            "status code: 500",
            "overrun",
        )
        return any(n in low for n in needles)

    def work(key: str, prompt: DistillPrompt) -> dict[str, Any]:
        current_dest["value"] = prompt.destination
        _safe_print(
            f"\n>>> Rollout {key} ({prompt.kind}) {prompt.destination} "
            f"nights={prompt.nights} teacher={args.teacher}",
        )
        # Skip if no fixture for this destination (unless allow empty)
        if prompt.destination.strip().lower() not in by_dest and not args.allow_empty_fixtures:
            return {
                "key": key,
                "ok": False,
                "permanent_fail": False,
                "error": f"no_fixture_for_{prompt.destination}",
                "elapsed_sec": 0.0,
            }
        row = _run_one_rollout(
            prompt,
            teacher=args.teacher,
            tool_turn_teacher=tool_teacher,
            host=host,
            destination_hint=prompt.destination,
        )
        if (
            not row.get("ok")
            and fallback_teacher
            and _retriable_teacher_error(str(row.get("error") or ""))
        ):
            primary_err = str(row.get("error") or "")
            primary_sec = float(row.get("elapsed_sec") or 0)
            _safe_print(
                f"!!! Primary teacher failed ({primary_err[:120]}) — "
                f"retrying with {fallback_teacher}",
            )
            try:
                from travel_agent.ollama_lifecycle import ensure_ollama_running

                ensure_ollama_running(
                    restart=False,
                    model=fallback_teacher,
                    ensure_model=True,
                )
            except Exception as exc:
                _safe_print(f"!!! Ollama recover note: {exc}")
            # After a CUDA crash, avoid stacking 14B+7B tool teacher; use 7b alone
            fb_tool = (
                fallback_teacher
                if tool_teacher and "cuda" in primary_err.lower()
                else (tool_teacher or fallback_teacher)
            )
            row2 = _run_one_rollout(
                prompt,
                teacher=fallback_teacher,
                tool_turn_teacher=fb_tool,
                host=host,
                destination_hint=prompt.destination,
            )
            row2["fallback_from"] = args.teacher
            row2["fallback_error"] = primary_err[:500]
            row2["elapsed_sec"] = round(
                primary_sec + float(row2.get("elapsed_sec") or 0), 2
            )
            row2["teacher"] = fallback_teacher
            row = row2
            _safe_print(
                f"<<< {'OK' if row.get('ok') else 'FAIL'} {key} "
                f"(fallback {fallback_teacher}) in {row.get('elapsed_sec')}s "
                f"tools={row.get('n_tool_rounds')}",
            )
            return row

        _safe_print(
            f"<<< {'OK' if row.get('ok') else 'FAIL'} {key} "
            f"in {row.get('elapsed_sec')}s tools={row.get('n_tool_rounds')}",
        )
        return row

    try:
        pairs = [(p.key, p) for p in selected]
        runner.run(pairs, work, total_planned=len(selected), default_unit_sec=420.0)
    finally:
        uninstall()
        _safe_print(f"Fixture hits={replayer.hits} misses={replayer.misses}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
