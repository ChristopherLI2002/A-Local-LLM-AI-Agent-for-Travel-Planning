"""Flask UI server: guided trip wizard → Ollama travel agent."""

from __future__ import annotations

import argparse
import atexit
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from travel_agent.agent import TravelAgent
from travel_agent.config import settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")

_agent: TravelAgent | None = None
_agent_lock = threading.RLock()
_started = False
_model: str | None = None


def _get_agent(model: str | None = None) -> TravelAgent:
    global _agent, _started
    with _agent_lock:
        if _agent is None:
            _agent = TravelAgent(model=model or _model or settings.ollama_model)
        if not _started:
            _agent.start()
            _started = True
            atexit.register(_shutdown_agent)
        return _agent


def _shutdown_agent() -> None:
    global _agent, _started
    with _agent_lock:
        if _agent is not None:
            try:
                _agent.close()
            except Exception:
                pass
            _agent = None
            _started = False


def _build_query(
    destination: str,
    depart_date: str,
    return_date: str | None,
    budget_hkd: float,
    origin: str,
) -> str:
    dates = (
        f"{depart_date} to {return_date}"
        if return_date
        else f"departing {depart_date}"
    )
    return (
        f"Plan a round-trip from {origin} to {destination}, {dates}, "
        f"budget {budget_hkd:g} HKD. Use live Trip.com prices and stay within budget."
    )


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.post("/api/plan")
def plan():
    payload = request.get_json(silent=True) or {}
    destination = str(payload.get("destination") or "").strip()
    depart_date = str(payload.get("depart_date") or "").strip()
    return_raw = payload.get("return_date")
    return_date = str(return_raw).strip() if return_raw else None
    origin = str(payload.get("origin") or "HKG").strip() or "HKG"

    try:
        budget_hkd = float(payload.get("budget_hkd"))
    except (TypeError, ValueError):
        return jsonify({"error": "budget_hkd must be a number."}), 400

    if not destination:
        return jsonify({"error": "destination is required."}), 400
    if not depart_date:
        return jsonify({"error": "depart_date is required."}), 400
    if budget_hkd <= 0:
        return jsonify({"error": "budget_hkd must be positive."}), 400

    query = _build_query(destination, depart_date, return_date, budget_hkd, origin)

    try:
        with _agent_lock:
            agent = _get_agent()
            # Lock already held for start; call chat while holding lock so
            # Playwright stays single-threaded.
            plan_text = agent.chat(query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"plan": plan_text, "query": query})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the Voyage HTML trip wizard UI",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=7860, help="Bind port")
    parser.add_argument(
        "-m",
        "--model",
        default=settings.ollama_model,
        help=f"Ollama model (default: {settings.ollama_model})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the Trip.com browser without a visible window",
    )
    parser.add_argument(
        "--lazy-browser",
        action="store_true",
        help="Start Playwright on first plan request (default: at server start)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global _model
    args = build_parser().parse_args(argv)
    _model = args.model
    if args.headless:
        settings.headless = True

    # Warm the agent unless lazy mode is requested.
    if not args.lazy_browser:
        print("Starting Trip.com browser…")
        _get_agent(model=args.model)
        print("Browser ready.")

    print(f"Voyage UI → http://{args.host}:{args.port}")
    # threaded=False keeps Playwright/sync agent safer.
    app.run(host=args.host, port=args.port, threaded=False, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
