"""Print llama.cpp setup instructions for GGUF export (Phase 5).

Compiling on Windows is environment-specific; this helper only prepares a
directory and documents the expected binaries.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = Path("D:/llama.cpp")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    args.dir.mkdir(parents=True, exist_ok=True)
    readme = args.dir / "DISTILL_SETUP.txt"
    readme.write_text(
        """llama.cpp setup for distill export
=================================

1) Clone:  git clone https://github.com/ggerganov/llama.cpp "%DIR%"
2) Build on Windows (one of):
   - CMake + Visual Studio, or
   - Download a release zip with llama-quantize.exe from GitHub Releases
3) Ensure these are on PATH (or set LLAMA_CPP_DIR):
   - convert_hf_to_gguf.py  (from the repo)
   - llama-quantize.exe

Then run:
  python -m distill.export_gguf --config distill/configs/student-1_5b.yaml --i-understand-this-exports
""".replace("%DIR%", str(args.dir)),
        encoding="utf-8",
    )
    print(f"Wrote {readme}")
    print("Install llama.cpp into that folder before Phase 5 export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
