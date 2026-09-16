# Distillation pipeline (black-box teacher → small student)

See the project plan for full timing. Quick start:

```powershell
# Phase 0 — paths on D: (C: is tight)
python -m distill.setup_env --setx
# new terminal, then:
$env:OLLAMA_MODELS = "D:\Ollama\models"
$env:HF_HOME = "D:\hf-cache"
$env:DISTILL_OUT = "D:\distill-out"
ollama pull qwen2.5:14b-instruct-q4_K_M
pip install -r requirements-distill.txt
python -m distill.calibrate

# Phase 1 — fixtures (resume across 1h sessions)
python -m distill.record_fixtures --max-hours 1
python -m distill.record_fixtures --resume --max-hours 1

# Phase 2 — teacher rollouts (default teacher: qwen2.5:7b)
python -m distill.teacher_rollout --max-hours 1 --target-rollouts 200
python -m distill.teacher_rollout --resume --max-hours 1

# Phase 3 — repair Day N: gaps (7B often skips them), then score + dataset
python -m distill.repair_rollouts
python -m distill.score --rewrite
python -m distill.build_dataset --val-split-by destination

# Phase 4 — GATED training (requires explicit flag; 1h sessions)
python -m distill.train_sft --config distill/configs/student-0_5b.yaml --max-hours 1 --i-understand-this-starts-training
python -m distill.train_sft --config distill/configs/student-1_5b.yaml --max-hours 1 --resume --i-understand-this-starts-training
```

Training will not start without `--i-understand-this-starts-training`.

## Newest checkpoint locations

Sessions stop every **1 hour** by default (`--max-hours 1`). Progress is always resume-safe via JSONL; a small status pointer is updated after each successful unit:

```text
Newest Phase 2 pointer: distill/data/checkpoints/teacher_rollout_latest.json
Newest Phase 2 data:    distill/data/raw_rollouts.jsonl

Newest Phase 1 pointer: distill/data/checkpoints/fixtures_latest.json
Newest Phase 1 data:    distill/data/tool_fixtures.jsonl

Training checkpoints:   D:\distill-out\student-*\checkpoint-<step>\
```

Resume Phase 2:

```powershell
python -m distill.teacher_rollout --resume --max-hours 1 --target-rollouts 200
```

Optional stronger teacher (slower / less stable on 6 GB VRAM):

```powershell
python -m distill.teacher_rollout --resume --max-hours 1 --teacher qwen2.5:14b-instruct-q4_K_M --fallback-teacher qwen2.5:7b
```

## Shipping student (current default)

`.env` defaults to `voyage-student-1-5b-dayfix` with `STUDENT_MODE=true` and `OLLAMA_NUM_CTX=16384`.

```powershell
# Fixture replay bench (fast, no Playwright)
python -m distill.bench --models voyage-student-1-5b-dayfix --student-tags voyage-student-1-5b-dayfix --limit 20

# Live Trip.com smoke (real scrapes; ~3–4 min each)
python -m distill.live_smoke --limit 3
```

Live results land under `D:\distill-out\live_smoke\`.
