"""Paths for distill data and outputs (prefer D: for large artifacts)."""

from __future__ import annotations

import os
from pathlib import Path

# Repo root: distill/paths.py → parent.parent
REPO_ROOT = Path(__file__).resolve().parent.parent

DISTILL_DIR = Path(__file__).resolve().parent
DATA_DIR = DISTILL_DIR / "data"
CONFIGS_DIR = DISTILL_DIR / "configs"
CHECKPOINTS_DIR = DATA_DIR / "checkpoints"

# Large artifacts on D: when available (plan: C: is tight)
_DEFAULT_OUT = Path("D:/distill-out")
# Canonical store (tray app); D:/ollama-models is a junction to the same path
_DEFAULT_OLLAMA = Path("D:/Ollama/models")
_DEFAULT_HF = Path("D:/hf-cache")


def out_dir() -> Path:
    raw = (os.getenv("DISTILL_OUT") or "").strip()
    p = Path(raw) if raw else _DEFAULT_OUT
    p.mkdir(parents=True, exist_ok=True)
    return p


def ollama_models_dir() -> Path:
    raw = (os.getenv("OLLAMA_MODELS") or "").strip()
    return Path(raw) if raw else _DEFAULT_OLLAMA


def hf_home() -> Path:
    raw = (os.getenv("HF_HOME") or "").strip()
    return Path(raw) if raw else _DEFAULT_HF


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "transcripts").mkdir(parents=True, exist_ok=True)
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    out_dir()


TOOL_FIXTURES_PATH = DATA_DIR / "tool_fixtures.jsonl"
RAW_ROLLOUTS_PATH = DATA_DIR / "raw_rollouts.jsonl"
SCORED_PATH = DATA_DIR / "scored_rollouts.jsonl"
TRAIN_PATH = DATA_DIR / "train.jsonl"
VAL_PATH = DATA_DIR / "val.jsonl"
CALIBRATE_PATH = DATA_DIR / "calibrate.json"

FIXTURES_STATUS_PATH = CHECKPOINTS_DIR / "fixtures_latest.json"
TEACHER_STATUS_PATH = CHECKPOINTS_DIR / "teacher_rollout_latest.json"
BENCH_STATUS_PATH = CHECKPOINTS_DIR / "bench_latest.json"
