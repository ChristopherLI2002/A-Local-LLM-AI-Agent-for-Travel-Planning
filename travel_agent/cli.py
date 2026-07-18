"""Interactive CLI for the Trip.com + Ollama travel agent."""

from __future__ import annotations

import argparse
import os
import sys

# Avoid Windows cp1252 crashes on tool/status output
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from travel_agent.agent import TravelAgent
from travel_agent.config import settings

console = Console(force_terminal=True, soft_wrap=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI travel agent using Ollama + Trip.com Hong Kong browser search",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=settings.ollama_model,
        help=f"Ollama model (default: {settings.ollama_model})",
    )
    parser.add_argument(
        "-q",
        "--query",
        help="Single-shot question (non-interactive)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser without a visible window",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Open the HTML trip wizard UI (destination → date → budget)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web UI bind host")
    parser.add_argument("--port", type=int, default=7860, help="Web UI bind port")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.web:
        from travel_agent.web_server import main as web_main

        web_args = ["--host", args.host, "--port", str(args.port), "-m", args.model]
        if args.headless:
            web_args.append("--headless")
        return web_main(web_args)

    if args.headless:
        settings.headless = True

    console.print(
        Panel.fit(
            f"[bold]Trip.com Travel Agent[/bold]\n"
            f"Model: [cyan]{args.model}[/cyan]\n"
            f"Site: [cyan]{settings.trip_base_url}[/cyan] "
            f"({settings.trip_locale}, {settings.trip_currency})\n"
            f"Features: [cyan]flight compare[/cyan] · "
            f"[cyan]hotel compare[/cyan] · [cyan]trip planning[/cyan]\n"
            f"Commands: [dim]/exit[/dim], [dim]/reset[/dim], [dim]/home[/dim]",
            border_style="blue",
        )
    )

    agent = TravelAgent(
        model=args.model,
        on_tool_start=lambda name, args_: console.print(
            f"[yellow]-> tool[/yellow] [bold]{name}[/bold] {args_}"
        ),
        on_tool_end=lambda name, preview: console.print(
            f"[green]<- {name}[/green]\n[dim]{preview}[/dim]\n"
        ),
    )

    try:
        console.print("[dim]Starting browser on Trip.com Hong Kong...[/dim]")
        agent.start()
        console.print("[green]Browser ready.[/green]\n")
    except Exception as exc:
        console.print(f"[red]Failed to start browser:[/red] {exc}")
        console.print(
            "Install Chromium with: [bold]playwright install chromium[/bold]"
        )
        return 1

    try:
        if args.query:
            _run_one(agent, args.query)
            return 0

        console.print(
            "Try:\n"
            "  [italic]Compare HKG-TPE flight prices for 2026-08-15, 2026-08-20, 2026-08-25[/italic]\n"
            "  [italic]Compare hotel prices in Taipei vs Taichung for Aug 20-22[/italic]\n"
            "  [italic]Plan a 5-day Tokyo trip from Hong Kong for 2 adults, budget HK$12000[/italic]\n"
        )
        while True:
            try:
                user = console.input("[bold blue]You>[/bold blue] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nBye.")
                break

            if not user:
                continue
            if user.lower() in {"/exit", "exit", "quit", "/quit"}:
                console.print("Bye.")
                break
            if user.lower() in {"/reset", "reset"}:
                agent.reset()
                console.print("[dim]Conversation reset.[/dim]")
                continue
            if user.lower() in {"/home", "home"}:
                msg = agent.browser.open_home()
                console.print(f"[dim]{msg}[/dim]")
                continue

            _run_one(agent, user)
    finally:
        agent.close()

    return 0


def _run_one(agent: TravelAgent, query: str) -> None:
    console.print("[dim]Thinking / searching Trip.com...[/dim]")
    try:
        answer = agent.chat(query)
    except Exception as exc:
        console.print(f"[red]Agent error:[/red] {exc}")
        return
    # Prefer plain text if markdown rendering hits encoding issues
    try:
        console.print(Panel(Markdown(answer), title="Agent", border_style="green"))
    except Exception:
        console.print(Panel(answer, title="Agent", border_style="green"))


if __name__ == "__main__":
    sys.exit(main())
