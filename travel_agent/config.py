"""Configuration for the travel agent."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    trip_base_url: str = os.getenv("TRIP_BASE_URL", "https://hk.trip.com")
    trip_locale: str = os.getenv("TRIP_LOCALE", "en_hk")
    trip_currency: str = os.getenv("TRIP_CURRENCY", "HKD")
    headless: bool = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes"}
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "45000"))
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "16"))


settings = Settings()


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
            f"Voyage will try to download {want or 'qwen2.5:7b'} on startup."
        )
    if want:
        for name in chat_models:
            if name == want or name.startswith(want + "-") or name.startswith(want + ":"):
                return name
        for name in chat_models:
            if want.split(":")[0] and name.startswith(want.split(":")[0]):
                return name
    return chat_models[0]
