"""Voyage desktop launcher (used by PyInstaller and for local runs)."""

from __future__ import annotations

import os
import sys


def _default_browsers_path() -> str:
    """Absolute Playwright browser cache (not under the frozen _internal tree)."""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    return os.path.join(local, "ms-playwright")


def _prepare_env() -> None:
    """Point Playwright at the shared browser cache before any playwright import."""
    # Absolute path — "0" is unreliable with some Playwright + PyInstaller setups.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _default_browsers_path()

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.chdir(exe_dir)
        # httpx/ollama need a CA bundle inside the PyInstaller _internal tree.
        meipass = getattr(sys, "_MEIPASS", "")
        for base in (meipass, exe_dir):
            if not base:
                continue
            for rel in ("certifi/cacert.pem", "cacert.pem"):
                ca = os.path.join(base, rel)
                if os.path.isfile(ca):
                    os.environ.setdefault("SSL_CERT_FILE", ca)
                    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
                    break


def main() -> int:
    _prepare_env()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from travel_agent.cli import main as cli_main

    return int(cli_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
