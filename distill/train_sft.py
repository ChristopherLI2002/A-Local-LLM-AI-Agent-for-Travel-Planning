"""Phase 4: QLoRA / LoRA SFT for student models.

GATED — do not run until train.jsonl exists and the user explicitly asks.

Resume:
  python -m distill.train_sft --config distill/configs/student-1_5b.yaml --max-hours 1 --resume
  python -m distill.train_sft --config distill/configs/student-1_5b.yaml --resume-from D:/distill-out/student-1_5b/checkpoint-400
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Force PyTorch-only path — avoid Keras 3 / tf_keras import failures on Windows.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TimeBudgetCallback:
    """HF TrainerCallback that saves then stops when --max-hours is reached."""

    def __init__(self, max_hours: float) -> None:
        from transformers import TrainerCallback

        # Build a real subclass dynamically so import is lazy
        self._base = TrainerCallback
        self.max_hours = max(0.01, float(max_hours))
        self.t0 = time.monotonic()
        self._stopped = False

    def on_step_end(self, args, state, control, **kwargs):  # noqa: ANN001, ARG002
        elapsed = time.monotonic() - self.t0
        if elapsed >= self.max_hours * 3600.0 and not self._stopped:
            self._stopped = True
            print(
                f"\n[TimeBudgetCallback] {self.max_hours}h reached — "
                "saving checkpoint then stopping.",
                flush=True,
            )
            control.should_save = True
            control.should_training_stop = True
        return control


def _make_time_budget_callback(max_hours: float):
    from transformers import TrainerCallback

    class _CB(TrainerCallback):
        def __init__(self) -> None:
            self.max_hours = max(0.01, float(max_hours))
            self.t0 = time.monotonic()
            self._stopped = False

        def on_step_end(self, args, state, control, **kwargs):  # noqa: ANN001, ARG002
            elapsed = time.monotonic() - self.t0
            if elapsed >= self.max_hours * 3600.0 and not self._stopped:
                self._stopped = True
                print(
                    f"\n[TimeBudgetCallback] {self.max_hours}h reached — "
                    "saving checkpoint then stopping.",
                    flush=True,
                )
                control.should_save = True
                control.should_training_stop = True
            return control

    return _CB()


def _format_example(example: dict[str, Any], tokenizer: Any) -> dict[str, str]:
    """Render messages to a single ChatML string for SFT."""
    messages = example.get("messages") or []
    # Prefer tokenizer chat template
    try:
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False,
            tools=example.get("tools"),
        )
    except TypeError:
        # Older transformers without tools= kw
        try:
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
        except Exception:
            text = _manual_chatml(messages)
    except Exception:
        text = _manual_chatml(messages)
    return {"text": text}


def _torch_needs_resume_stash() -> bool:
    """True when transformers will refuse torch.load of optimizer.pt."""
    import torch

    parts = torch.__version__.split("+")[0].split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return True
    return (major, minor) < (2, 6)


def _resolve_resume_checkpoint(output_dir: Path, resume_from: str | bool) -> Path | None:
    if isinstance(resume_from, str) and resume_from not in ("", "True", "true"):
        p = Path(resume_from)
        return p if p.exists() else None
    cps = sorted(
        (p for p in output_dir.glob("checkpoint-*") if p.is_dir()),
        key=lambda p: int(p.name.split("-")[-1]),
    )
    return cps[-1] if cps else None


def _stash_torch_load_sidecars(ckpt: Path) -> list[tuple[Path, Path]]:
    """Temporarily hide optimizer/scheduler/rng .pt so resume can use safetensors."""
    stashed: list[tuple[Path, Path]] = []
    for name in ("optimizer.pt", "scheduler.pt", "rng_state.pth"):
        src = ckpt / name
        if not src.exists():
            continue
        bak = ckpt / f"{name}.bak_torch25"
        if bak.exists():
            bak.unlink()
        src.rename(bak)
        stashed.append((src, bak))
    return stashed



def _manual_chatml(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role") or "user"
        content = m.get("content") or ""
        if m.get("tool_calls"):
            content = (content or "") + "\n" + json.dumps(m["tool_calls"], ensure_ascii=False)
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Student SFT (gated — explicit run only)")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--max-hours", type=float, default=1.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-from", type=str, default="")
    parser.add_argument(
        "--init-adapter",
        type=Path,
        default=None,
        help="Load an existing LoRA adapter (e.g. .../final) instead of random LoRA init",
    )
    parser.add_argument("--max-seq-len", type=int, default=0, help="Override config max_seq_length")
    parser.add_argument(
        "--i-understand-this-starts-training",
        action="store_true",
        help="Required safety flag to actually launch training",
    )
    args = parser.parse_args(argv)

    if not args.i_understand_this_starts_training:
        print(
            "Refusing to start training without --i-understand-this-starts-training.\n"
            "Training is gated by the distill plan. When ready:\n"
            f"  python -m distill.train_sft --config {args.config} --max-hours 1 "
            "--i-understand-this-starts-training\n"
            "Resume later with --resume (same flag required)."
        )
        return 3

    cfg = _load_yaml(args.config)
    train_file = Path(cfg["train_file"])
    if not train_file.is_absolute():
        train_file = _ROOT / train_file
    val_file = Path(cfg.get("val_file") or "")
    if val_file and not val_file.is_absolute():
        val_file = _ROOT / val_file
    if not train_file.exists():
        print(f"Missing train file: {train_file}. Build dataset first.")
        return 1

    max_seq = int(args.max_seq_len or cfg.get("max_seq_length") or 4096)
    output_dir = Path(cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    model_id = cfg["model_name_or_path"]
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quant = None
    if cfg.get("load_in_4bit") or cfg.get("use_qlora"):
        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )

    model_kwargs: dict[str, Any] = dict(
        quantization_config=quant,
        device_map="auto",
        trust_remote_code=True,
    )
    # transformers ≥4.56 prefers dtype=; older uses torch_dtype=
    try:
        model = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=torch.bfloat16, **model_kwargs
        )
    except TypeError:
        model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, **model_kwargs
        )
    if quant is not None:
        model = prepare_model_for_kbit_training(model)

    init_adapter = args.init_adapter
    if init_adapter is None and cfg.get("init_adapter"):
        init_adapter = Path(str(cfg["init_adapter"]))
    if init_adapter is not None:
        from peft import PeftModel

        print(f"Loading existing adapter from {init_adapter}", flush=True)
        model = PeftModel.from_pretrained(model, str(init_adapter), is_trainable=True)
    else:
        lora = LoraConfig(
            r=int(cfg.get("lora_r") or 32),
            lora_alpha=int(cfg.get("lora_alpha") or 64),
            lora_dropout=float(cfg.get("lora_dropout") or 0.05),
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        )
        model = get_peft_model(model, lora)
    if cfg.get("gradient_checkpointing", True):
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()

    data_files = {"train": str(train_file)}
    if val_file and val_file.exists():
        data_files["validation"] = str(val_file)
    ds = load_dataset("json", data_files=data_files)
    ds = ds.map(lambda ex: _format_example(ex, tokenizer), remove_columns=ds["train"].column_names)

    sft_args = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=float(cfg.get("num_train_epochs") or 3),
        per_device_train_batch_size=int(cfg.get("per_device_train_batch_size") or 1),
        gradient_accumulation_steps=int(cfg.get("gradient_accumulation_steps") or 16),
        learning_rate=float(cfg.get("learning_rate") or 1e-4),
        warmup_ratio=float(cfg.get("warmup_ratio") or 0.03),
        logging_steps=int(cfg.get("logging_steps") or 10),
        save_steps=int(cfg.get("save_steps") or 100),
        save_total_limit=int(cfg.get("save_total_limit") or 3),
        bf16=bool(cfg.get("bf16", True)),
        optim=str(cfg.get("optim") or "adamw_torch"),
        lr_scheduler_type=str(cfg.get("lr_scheduler_type") or "cosine"),
        report_to=[],
        seed=int(cfg.get("seed") or 0),
        remove_unused_columns=False,
        dataloader_pin_memory=False,
        max_length=max_seq,
        dataset_text_field="text",
        packing=False,
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_args,
        train_dataset=ds["train"],
        eval_dataset=ds["validation"] if "validation" in ds else None,
        processing_class=tokenizer,
        callbacks=[_make_time_budget_callback(args.max_hours)],
    )

    resume_from: str | bool | None = None
    if args.resume_from:
        resume_from = args.resume_from
    elif args.resume:
        resume_from = True

    # transformers≥4.5x requires torch≥2.6 to torch.load optimizer.pt (CVE-2025-32434).
    # On torch 2.5.x, resume adapter + trainer_state by stashing unsafe .pt sidecars.
    stashed: list[tuple[Path, Path]] = []
    if resume_from:
        ckpt_path = _resolve_resume_checkpoint(output_dir, resume_from)
        if ckpt_path is not None and _torch_needs_resume_stash():
            stashed = _stash_torch_load_sidecars(ckpt_path)
            if stashed:
                print(
                    f"torch {torch.__version__} < 2.6: resuming weights/state from "
                    f"{ckpt_path.name} without optimizer/scheduler (.pt stashed).",
                    flush=True,
                )
            resume_from = str(ckpt_path)

    try:
        trainer.train(resume_from_checkpoint=resume_from)
    finally:
        for src, bak in stashed:
            if bak.exists() and not src.exists():
                bak.rename(src)

    trainer.save_model(str(output_dir / "final"))
    tokenizer.save_pretrained(str(output_dir / "final"))
    print(f"Saved to {output_dir / 'final'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
