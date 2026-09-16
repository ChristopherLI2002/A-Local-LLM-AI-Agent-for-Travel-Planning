"""One-shot Phase 0 path setup helpers (Windows).

Ensures D:\\Ollama\\models (or OLLAMA_MODELS), D:\\hf-cache, D:\\distill-out and prints setx commands.
Does not pull models (network) unless --pull is passed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.paths import ensure_data_dirs, hf_home, ollama_models_dir, out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", action="store_true", help="ollama pull teacher")
    parser.add_argument(
        "--teacher",
        default="qwen2.5:14b-instruct-q4_K_M",
    )
    parser.add_argument(
        "--setx",
        action="store_true",
        help="Call setx to persist OLLAMA_MODELS and HF_HOME (new terminals only)",
    )
    args = parser.parse_args(argv)

    for p in (ollama_models_dir(), hf_home(), out_dir()):
        p.mkdir(parents=True, exist_ok=True)
        print(f"Ensured {p}")
    ensure_data_dirs()

    # Process-local env for this shell's children
    os.environ["OLLAMA_MODELS"] = str(ollama_models_dir())
    os.environ["HF_HOME"] = str(hf_home())
    os.environ.setdefault("DISTILL_OUT", str(out_dir()))

    if args.setx:
        subprocess.run(["setx", "OLLAMA_MODELS", str(ollama_models_dir())], check=False)
        subprocess.run(["setx", "HF_HOME", str(hf_home())], check=False)
        subprocess.run(["setx", "DISTILL_OUT", str(out_dir())], check=False)
        print("setx done — open a new terminal for persistence.")

    print(
        "\nManual (PowerShell current session):\n"
        f'  $env:OLLAMA_MODELS = "{ollama_models_dir()}"\n'
        f'  $env:HF_HOME = "{hf_home()}"\n'
        f'  $env:DISTILL_OUT = "{out_dir()}"\n'
    )

    if args.pull:
        print(f"Pulling {args.teacher} …")
        r = subprocess.run(["ollama", "pull", args.teacher])
        return int(r.returncode)

    print("Skip pull (pass --pull). Next: pip install -r requirements-distill.txt")
    print("Then: python -m distill.calibrate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
