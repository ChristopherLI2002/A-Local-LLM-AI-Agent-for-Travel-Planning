"""Configuration for the travel agent."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    # Lighter default for faster local responses (still tool-capable).
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    trip_base_url: str = os.getenv("TRIP_BASE_URL", "https://hk.trip.com")
    trip_locale: str = os.getenv("TRIP_LOCALE", "en_hk")
    trip_currency: str = os.getenv("TRIP_CURRENCY", "HKD")
    headless: bool = _env_bool("HEADLESS", True)
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "45000"))
    # Fewer agent↔tool loops = faster end-to-end plans
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
    # Fast mode: skip extra LLM ranking/attraction-arrange calls; use heuristics
    fast_mode: bool = _env_bool("FAST_MODE", True)
    # When false (or fast_mode), pick cheapest/first scraped option without Ollama
    llm_rank_candidates: bool = _env_bool("LLM_RANK_CANDIDATES", False)
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
    ollama_num_predict: int = int(os.getenv("OLLAMA_NUM_PREDICT", "768"))
    ollama_temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))


settings = Settings()


def ollama_chat_options(*, short: bool = False) -> dict:
    """Shared Ollama generation options tuned for local speed."""
    opts: dict = {
        "temperature": settings.ollama_temperature,
        "num_ctx": max(1024, int(settings.ollama_num_ctx)),
    }
    if short:
        opts["num_predict"] = min(96, max(32, int(settings.ollama_num_predict) // 4))
    else:
        opts["num_predict"] = max(128, int(settings.ollama_num_predict))
    return opts


def list_ollama_models(host: str | None = None) -> list[str]:
    """Return installed Ollama model names (empty if Ollama is down)."""
    # Prefer HTTP /api/tags — more reliable than the Python client shape
    try:
        from travel_agent.ollama_lifecycle import fetch_ollama_model_names

        names = fetch_ollama_model_names(host)
        if names:
            return names
    except Exception:
        pass
    try:
        import ollama

        client = ollama.Client(host=host or settings.ollama_host)
        raw = client.list()
    except Exception:
        return []
    names: list[str] = []
    models = raw.get("models") if isinstance(raw, dict) else getattr(raw, "models", None)
    for item in models or []:
        if isinstance(item, dict):
            name = (item.get("name") or item.get("model") or "").strip()
        else:
            name = (
                getattr(item, "model", None) or getattr(item, "name", None) or ""
            ).strip()
        if name:
            names.append(name)
    return names


def resolve_ollama_model(
    preferred: str | None = None,
    *,
    host: str | None = None,
) -> str:
    """Use preferred model if installed; otherwise first installed chat tag.

    Raises RuntimeError when no models are available. Prefer calling
    ``ensure_ollama_running(..., ensure_model=True)`` at app start so the model
    is pulled automatically instead of failing here.
    """
    want = (preferred or settings.ollama_model or "").strip()
    installed = list_ollama_models(host)
    # Skip embedding-only tags when falling back
    chat_models = [
        n
        for n in installed
        if "embed" not in n.lower()
    ] or installed
    if not chat_models:
        raise RuntimeError(
            f"No Ollama models installed at {host or settings.ollama_host}. "
            f"Voyage will try to download {want or 'qwen2.5:3b'} on startup."
        )
    if want:
        for name in chat_models:
            if name == want or name.startswith(want + "-") or name.startswith(want + ":"):
                return name
        for name in chat_models:
            if want.split(":")[0] and name.startswith(want.split(":")[0]):
                return name
    return chat_models[0]
