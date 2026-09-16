"""Phase 5: merge LoRA → fp16, convert notes for GGUF / Ollama (gated).

This script merges adapters. GGUF conversion requires llama.cpp binaries on PATH
or LLAMA_CPP_DIR. Quantize and `ollama create` are printed as follow-up commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from distill.compact_prompt import COMPACT_SYSTEM_PROMPT


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_modelfile(path: Path, *, gguf_name: str, system: str) -> None:
    # Qwen2.5 tool-capable chat template (simplified; Ollama may override)
    template = """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{- range .Messages }}{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ .Content }}<|im_end|>
{{ end }}{{- end }}<|im_start|>assistant
"""
    body = (
        f"FROM ./{gguf_name}\n\n"
        f'SYSTEM """{system}"""\n\n'
        f'TEMPLATE """{template}"""\n\n'
        "PARAMETER temperature 0.2\n"
        "PARAMETER num_ctx 16384\n"
        "PARAMETER stop <|im_end|>\n"
        "PARAMETER stop <|endoftext|>\n"
    )
    path.write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge student LoRA and emit Modelfile")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--adapter-dir",
        type=Path,
        default=None,
        help="Defaults to <output_dir>/final",
    )
    parser.add_argument(
        "--i-understand-this-exports",
        action="store_true",
        help="Required safety flag",
    )
    args = parser.parse_args(argv)

    if not args.i_understand_this_exports:
        print(
            "Refusing without --i-understand-this-exports.\n"
            f"  python -m distill.export_gguf --config {args.config} --i-understand-this-exports"
        )
        return 3

    cfg = _load_yaml(args.config)
    output_dir = Path(cfg["output_dir"])
    adapter = args.adapter_dir or (output_dir / "final")
    if not adapter.exists():
        print(f"Adapter dir missing: {adapter}")
        return 1

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    base_id = cfg["model_name_or_path"]
    print(f"Loading base {base_id} …")
    tokenizer = AutoTokenizer.from_pretrained(adapter if (adapter / "tokenizer_config.json").exists() else base_id)
    base = AutoModelForCausalLM.from_pretrained(
        base_id, torch_dtype=torch.float16, device_map="cpu", trust_remote_code=True
    )
    model = PeftModel.from_pretrained(base, str(adapter))
    merged = model.merge_and_unload()
    merge_dir = output_dir / "merged-fp16"
    merge_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(str(merge_dir))
    tokenizer.save_pretrained(str(merge_dir))
    print(f"Merged weights → {merge_dir}")

    tag = output_dir.name.replace("_", "-")
    gguf_name = f"{tag}.q4_k_m.gguf"
    modelfile = output_dir / "Modelfile"
    write_modelfile(modelfile, gguf_name=gguf_name, system=COMPACT_SYSTEM_PROMPT)
    print(f"Wrote {modelfile}")
    print(
        "\nNext (requires llama.cpp convert + quantize on PATH):\n"
        f"  python convert_hf_to_gguf.py {merge_dir} --outfile {output_dir / (tag + '.f16.gguf')}\n"
        f"  llama-quantize {output_dir / (tag + '.f16.gguf')} {output_dir / gguf_name} Q4_K_M\n"
        f"  cd {output_dir} && ollama create voyage-{tag} -f Modelfile\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
