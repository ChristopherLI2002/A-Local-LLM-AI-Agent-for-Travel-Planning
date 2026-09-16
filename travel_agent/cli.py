"""Entry point: desktop GUI by default, optional terminal chat."""

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

from travel_agent.config import settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A local LLM AI agent for travel planning (Ollama + Trip.com Hong Kong)",
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
        help="Single-shot question in the terminal (skips the window UI)",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use terminal chat instead of the desktop window",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright without a visible window (default for the desktop app)",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show the Playwright Chromium window while scraping Trip.com",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from travel_agent.config import apply_model_settings

    apply_model_settings(args.model)

    # Desktop window is the default unless --cli or -q is used.
    if not args.cli and not args.query:
        from travel_agent.gui import main as gui_main

        gui_args = ["-m", args.model]
        if args.show_browser:
            gui_args.append("--show-browser")
        return gui_main(gui_args)

    return _run_cli(args)


def _run_cli(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    from travel_agent.agent import TravelAgent

    console = Console(force_terminal=True, soft_wrap=True)

    if args.headless:
        settings.headless = True
    if getattr(args, "show_browser", False):
        settings.headless = False

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

    try:
        from travel_agent.ollama_lifecycle import ensure_ollama_running

        console.print("[dim]Starting Ollama…[/dim]")
        resolved = ensure_ollama_running(
            restart=False,
            on_progress=lambda m: console.print(f"[dim]{m}[/dim]"),
            model=args.model,
            ensure_model=True,
        )
        if resolved:
            args.model = resolved
    except Exception as exc:
        console.print(f"[red]Failed to start Ollama:[/red] {exc}")
        return 1

    agent = TravelAgent(
        model=args.model,
        on_tool_start=lambda name, args_: console.print(
            f"[yellow]-> tool[/yellow] [bold]{name}[/bold] {args_}"
        ),
        on_tool_end=lambda name, preview: console.print(
            f"[green]<- {name}[/green]\n[dim]{preview}[/dim]\n"
        ),
        on_status=lambda msg: console.print(f"[dim]{msg}[/dim]"),
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
            _run_one(console, agent, args.query)
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

            _run_one(console, agent, user)
    finally:
        try:
            agent.close()
        except Exception:
            pass
        # Leave Ollama running for faster relaunches

    return 0


def _run_one(console, agent, query: str) -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel

    console.print("[dim]Thinking / searching Trip.com...[/dim]")
    try:
        answer = agent.chat(query)
    except Exception as exc:
        console.print(f"[red]Agent error:[/red] {exc}")
        return
    try:
        console.print(Panel(Markdown(answer), title="Agent", border_style="green"))
    except Exception:
        console.print(Panel(answer, title="Agent", border_style="green"))


if __name__ == "__main__":
    sys.exit(main())
