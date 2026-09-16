"""Phase 0 helper: measure teacher throughput and print a corrected schedule."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.paths import CALIBRATE_PATH, ensure_data_dirs, hf_home, ollama_models_dir, out_dir


def _measure_ollama(model: str, host: str) -> dict:
    from travel_agent.config import make_ollama_client, ollama_chat_options

    client = make_ollama_client(host)
    # Prefill-ish: long system + short completion
    long_prompt = ("Plan a detailed trip. " * 80) + "Reply with OK only."
    t0 = time.perf_counter()
    r1 = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": "You are a concise travel assistant."},
            {"role": "user", "content": long_prompt},
        ],
        options={**ollama_chat_options(short=True), "num_predict": 8},
    )
    prefill_wall = time.perf_counter() - t0
    # Decode: ask for ~200 tokens
    t1 = time.perf_counter()
    r2 = client.chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a 150-word day-by-day Tokyo itinerary with clock times "
                    "and named places. No tools."
                ),
            }
        ],
        options={**ollama_chat_options(short=False), "num_predict": 256},
    )
    decode_wall = time.perf_counter() - t1
    content = ((r2.get("message") or {}).get("content") or "")
    # Rough token estimate
    out_tokens = max(1, len(content) // 4)
    eval_count = r2.get("eval_count") or out_tokens
    prompt_count = r2.get("prompt_eval_count") or (len(long_prompt) // 4)
    decode_tps = float(eval_count) / max(1e-3, decode_wall)
    # Prefill rate from first call
    p_count = r1.get("prompt_eval_count") or (len(long_prompt) // 4)
    prefill_tps = float(p_count) / max(1e-3, prefill_wall)

    return {
        "model": model,
        "host": host,
        "prefill_tokens_est": int(p_count),
        "prefill_wall_sec": round(prefill_wall, 2),
        "prefill_tok_per_sec": round(prefill_tps, 2),
        "decode_tokens": int(eval_count),
        "decode_wall_sec": round(decode_wall, 2),
        "decode_tok_per_sec": round(decode_tps, 2),
        "prompt_eval_count_r2": r2.get("prompt_eval_count"),
        "sample_preview": content[:200],
    }


def _schedule(decode_tps: float) -> dict:
    # ~1060 output tokens per rollout + ~60s prefill overhead baseline
    decode_sec = 1060.0 / max(0.5, decode_tps)
    prefill_sec = 60.0 * (5.0 / max(1.0, decode_tps))  # scale vaguely with speed
    unit_sec = decode_sec + prefill_sec + 30.0  # tool dispatch overhead
    rollouts = 300
    best_of_extra = int(rollouts * 0.30)
    phase2_hours = (rollouts + best_of_extra) * unit_sec / 3600.0
    return {
        "est_unit_sec": round(unit_sec, 1),
        "est_phase2_hours_300": round(phase2_hours, 1),
        "est_phase2_sessions_3h": round(phase2_hours / 3.0, 1),
        "est_phase2_hours_200_with_7b_tools": round(phase2_hours * 0.6 * (200 / 300), 1),
        "note": "Estimates only; use --tool-turn-teacher qwen2.5:7b and --target-rollouts 200 to cut time.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate teacher throughput")
    parser.add_argument(
        "--model",
        default="qwen2.5:14b-instruct-q4_K_M",
        help="Teacher model tag (must be pulled)",
    )
    parser.add_argument(
        "--fallback-model",
        default="qwen2.5:7b",
        help="If primary missing, measure this instead",
    )
    args = parser.parse_args(argv)

    ensure_data_dirs()
    from travel_agent.config import list_ollama_models, settings

    print("Paths:")
    print(f"  OLLAMA_MODELS -> {ollama_models_dir()}")
    print(f"  HF_HOME       -> {hf_home()}")
    print(f"  DISTILL_OUT   -> {out_dir()}")
    print(f"  OLLAMA_HOST   -> {settings.ollama_host}")

    installed = list_ollama_models()
    print(f"Installed models: {len(installed)}")
    model = args.model
    if not any(model in m or m.startswith(model.split(":")[0]) for m in installed):
        # try exact / prefix
        if args.fallback_model and any(args.fallback_model in m for m in installed):
            print(f"WARNING: {model} not found; calibrating with {args.fallback_model}")
            model = args.fallback_model
        else:
            print(
                f"ERROR: model {model} not installed. Pull with:\n"
                f"  ollama pull {args.model}"
            )
            result = {
                "error": "model_not_found",
                "requested": args.model,
                "installed": installed,
                "paths": {
                    "OLLAMA_MODELS": str(ollama_models_dir()),
                    "HF_HOME": str(hf_home()),
                    "DISTILL_OUT": str(out_dir()),
                },
            }
            CALIBRATE_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return 2

    print(f"Measuring {model} …")
    try:
        metrics = _measure_ollama(model, settings.ollama_host)
    except Exception as exc:
        print(f"Measurement failed: {exc}")
        return 1

    sched = _schedule(float(metrics["decode_tok_per_sec"]))
    result = {"metrics": metrics, "schedule": sched, "installed": installed}
    CALIBRATE_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))
    print(f"\nWrote {CALIBRATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
