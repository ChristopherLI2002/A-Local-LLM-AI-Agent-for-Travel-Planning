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
    headless: bool = os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"}
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "45000"))
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "16"))


settings = Settings()
