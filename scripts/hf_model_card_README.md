---
base_model: Qwen/Qwen2.5-1.5B-Instruct
library_name: transformers
license: apache-2.0
language:
- en
tags:
- qwen2.5
- travel
- tool-use
- ollama
- gguf
- sft
- trip-planner
pipeline_tag: text-generation
---

# voyage-student-1-5b-dayfill

Distilled **1.5B** travel-planning student for the [Local LLM AI Agent for Travel Planning](https://github.com/ChristopherLI2002/An-AI-Agent-for-travel-planning) desktop app.

Fine-tuned from [Qwen/Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) with supervised fine-tuning (SFT) on Trip.Planner-style tool trajectories (route proposal → `plan_trip` → day-by-day itinerary). Optimized for **day-fill** itinerary structure and local **Ollama** serving.

## Files

| File | Use |
|------|-----|
| `student-1-5b-dayfill.q4_k_m.gguf` | Ollama / llama.cpp (Q4_K_M) — primary runtime artifact |
| `Modelfile` | Create the Ollama tag used by the app |
| Tokenizer / `config.json` | Reference metadata (full fp16 Transformers weights are not shipped in this revision) |

## Quick start (Ollama)

```bash
huggingface-cli download ChristopherLi/voyage-student-1-5b-dayfill \
  student-1-5b-dayfill.q4_k_m.gguf Modelfile \
  --local-dir ./voyage-student-1-5b-dayfill
cd voyage-student-1-5b-dayfill
ollama create voyage-student-1-5b-dayfill -f Modelfile
ollama run voyage-student-1-5b-dayfill
```

Point the travel agent at it (default):

```bash
# .env
OLLAMA_MODEL=voyage-student-1-5b-dayfill
STUDENT_MODE=true
OLLAMA_NUM_CTX=16384
```

```bash
python -m travel_agent
```

## Intended use

- Local travel itinerary drafting with tool calls against Trip.com scrapes (via the companion app).
- Not a booking engine; prices and URLs must come from live tools, not model invention.

## Training

- Base: Qwen2.5-1.5B-Instruct
- Method: SFT (TRL) on distilled teacher rollouts with day-fill emphasis
- Quantization shipped here: Q4_K_M GGUF for Ollama

## Citation / attribution

- Base model: Qwen2.5 by Alibaba
- App: [An-AI-Agent-for-travel-planning](https://github.com/ChristopherLI2002/An-AI-Agent-for-travel-planning)
