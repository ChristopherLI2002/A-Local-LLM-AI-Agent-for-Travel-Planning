"""Quick Ollama connectivity check for Voyage (Windows IPv4 localhost fix)."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from travel_agent.config import make_ollama_client, normalize_ollama_host, settings
from travel_agent.ollama_lifecycle import is_ollama_ready


def main() -> int:
    host = settings.ollama_host
    print(f"Configured OLLAMA_HOST: {host}")
    print(f"Normalized host:        {normalize_ollama_host(host)}")

    try:
        addrs = socket.getaddrinfo("localhost", 11434, proto=socket.IPPROTO_TCP)
        print("localhost resolves to:", [a[4][0] for a in addrs])
    except Exception as exc:
        print("localhost lookup failed:", exc)

    if not is_ollama_ready(host):
        print("\nFAIL: Ollama API is not reachable.")
        print("Start Ollama (app or `ollama serve`) and rerun this script.")
        return 1

    print("\nAPI /api/tags: OK")

    client = make_ollama_client(host)
    try:
        r = client.chat(
            model=settings.ollama_model,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
            options={"num_predict": 8},
        )
        text = (r.get("message") or {}).get("content", "").strip()
        print(f"Test chat ({settings.ollama_model}): OK -> {text!r}")
    except Exception as exc:
        print(f"\nFAIL: chat request failed: {exc}")
        print(
            "\nIf you see WinError 10054 or 502, set in .env:\n"
            "  OLLAMA_HOST=http://127.0.0.1:11434"
        )
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
