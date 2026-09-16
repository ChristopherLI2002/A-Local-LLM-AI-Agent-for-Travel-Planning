"""Configuration for the travel agent."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def app_dir() -> Path:
    """Directory containing the EXE (frozen) or the project root (dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # travel_agent/config.py → project root
    return Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Load .env from the app folder first, then the process cwd."""
    base = app_dir()
    load_dotenv(base / ".env")
    load_dotenv()  # cwd override if present


_load_env()

# Always use the shared user browser cache — never look under a frozen
# PyInstaller _internal/playwright/... tree (that path has no Chromium).
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ or not str(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
).strip():
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(_local) / "ms-playwright")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def normalize_ollama_host(host: str | None = None) -> str:
    """Use IPv4 loopback — ``localhost`` often resolves to ``::1`` on Windows
    while Ollama listens on ``127.0.0.1``, causing WinError 10054 / 502."""
    raw = (host or os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    return raw.replace("://localhost", "://127.0.0.1").replace("://localhost:", "://127.0.0.1:")


def make_ollama_client(host: str | None = None):
    """Shared Ollama client — bypasses system proxy for local API calls."""
    import ollama

    return ollama.Client(
        host=normalize_ollama_host(host),
        trust_env=False,
        timeout=300.0,
    )


DEFAULT_OLLAMA_MODEL = "voyage-student-1-5b-dayfix"


def is_student_model(name: str | None) -> bool:
    """True for distilled Voyage students (need compact prompt + slim tools)."""
    low = (name or "").strip().lower()
    return "voyage-student" in low or low.startswith("student-")


def _default_student_mode() -> bool:
    """STUDENT_MODE env wins; otherwise follow the configured model name."""
    raw = os.getenv("STUDENT_MODE")
    if raw is not None and str(raw).strip():
        return _env_bool("STUDENT_MODE", True)
    return is_student_model(os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))


@dataclass
class Settings:
    ollama_host: str = normalize_ollama_host()
    # Distilled student default; override with OLLAMA_MODEL / -m.
    ollama_model: str = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    trip_base_url: str = os.getenv("TRIP_BASE_URL", "https://hk.trip.com")
    trip_locale: str = os.getenv("TRIP_LOCALE", "en_hk")
    trip_currency: str = os.getenv("TRIP_CURRENCY", "HKD")
    headless: bool = _env_bool("HEADLESS", True)
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "45000"))
    # Fewer agent↔tool loops = faster end-to-end plans
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
    student_max_tool_rounds: int = int(os.getenv("STUDENT_MAX_TOOL_ROUNDS", "4"))
    student_tool_result_max_chars: int = int(
        os.getenv("STUDENT_TOOL_RESULT_MAX_CHARS", "3500")
    )
    # Fast mode: skip extra LLM ranking/attraction-arrange calls; use heuristics
    fast_mode: bool = _env_bool("FAST_MODE", True)
    # When false (or fast_mode), pick cheapest/first scraped option without Ollama
    llm_rank_candidates: bool = _env_bool("LLM_RANK_CANDIDATES", False)
    # Student needs a larger ctx for scrape+itinerary; base models can lower via .env
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
    ollama_num_predict: int = int(os.getenv("OLLAMA_NUM_PREDICT", "2048"))
    ollama_temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
    # Compact system prompt + slim tools (required for distilled students)
    student_mode: bool = _default_student_mode()


settings = Settings()


def apply_model_settings(model: str | None) -> None:
    """Keep runtime settings aligned when the user picks ``-m`` / GUI model."""
    name = (model or "").strip()
    if not name:
        return
    settings.ollama_model = name
    if is_student_model(name):
        # Distilled students must use compact prompt + slim tools.
        settings.student_mode = True
    else:
        # Non-students: only enable if .env explicitly requests it.
        raw = os.getenv("STUDENT_MODE")
        if raw is not None and str(raw).strip():
            settings.student_mode = _env_bool("STUDENT_MODE", False)
        else:
            settings.student_mode = False


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

        client = make_ollama_client(host or settings.ollama_host)
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
            f"The app will try to download {want or DEFAULT_OLLAMA_MODEL} on startup."
        )
    if want:
        for name in chat_models:
            if name == want or name.startswith(want + "-") or name.startswith(want + ":"):
                return name
        for name in chat_models:
            if want.split(":")[0] and name.startswith(want.split(":")[0]):
                return name
    return chat_models[0]
