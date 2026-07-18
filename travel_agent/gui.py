"""Desktop window UI for the Trip.com + Ollama travel agent (tkinter)."""

from __future__ import annotations

import argparse
import re
import threading
import tkinter as tk
import webbrowser
from datetime import date
from tkinter import messagebox, scrolledtext, ttk

from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.planner_query import build_plan_query

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Soft coastal palette (not purple / cream-terracotta defaults)
COLORS = {
    "bg": "#0f2740",
    "panel": "#163554",
    "card": "#1c4063",
    "accent": "#2bb0a6",
    "accent_dim": "#1f8a82",
    "text": "#eef5f8",
    "muted": "#9bb4c7",
    "input_bg": "#0b1d30",
    "danger": "#e07070",
    "ok": "#6dcb8d",
}


class TravelAgentApp(tk.Tk):
    def __init__(self, model: str, headless: bool = False) -> None:
        super().__init__()
        self.model = model
        self.headless = headless
        self.agent: TravelAgent | None = None
        self._busy = False

        self.title("Voyage — Trip.com Travel Agent")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.configure(bg=COLORS["bg"])

        self._style()
        self._build_chrome()
        self._build_tabs()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(100, self._boot_agent)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure(
            "TLabel",
            background=COLORS["bg"],
            foreground=COLORS["text"],
            font=("Segoe UI", 11),
        )
        style.configure(
            "Muted.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 22),
        )
        style.configure(
            "Brand.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["accent"],
            font=("Segoe UI Semibold", 28),
        )
        style.configure(
            "Card.TLabel",
            background=COLORS["card"],
            foreground=COLORS["text"],
            font=("Segoe UI", 11),
        )
        style.configure(
            "CardMuted.TLabel",
            background=COLORS["card"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "TCheckbutton",
            background=COLORS["card"],
            foreground=COLORS["text"],
            font=("Segoe UI", 10),
        )
        style.map(
            "TCheckbutton",
            background=[("active", COLORS["card"])],
            foreground=[("active", COLORS["text"])],
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", 11),
            padding=(16, 8),
        )
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            padding=(14, 8),
            font=("Segoe UI", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["card"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=COLORS["input_bg"],
            foreground=COLORS["text"],
            insertcolor=COLORS["text"],
            padding=8,
        )

    def _build_chrome(self) -> None:
        top = ttk.Frame(self, style="TFrame")
        top.pack(fill="x", padx=24, pady=(18, 8))
        ttk.Label(top, text="Voyage", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(
            top,
            text="Plan trips · compare flights & hotels · live Trip.com Hong Kong",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.status_var = tk.StringVar(value="Starting Trip.com browser…")
        self.status = ttk.Label(top, textvariable=self.status_var, style="Muted.TLabel")
        self.status.pack(anchor="w", pady=(10, 0))

    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=20, pady=(4, 16))

        self.plan_tab = ttk.Frame(self.nb, style="TFrame")
        self.chat_tab = ttk.Frame(self.nb, style="TFrame")
        self.nb.add(self.plan_tab, text="  Trip planner  ")
        self.nb.add(self.chat_tab, text="  Chat  ")

        self._build_planner(self.plan_tab)
        self._build_chat(self.chat_tab)

    def _build_planner(self, parent: ttk.Frame) -> None:
        form = ttk.Frame(parent, style="Card.TFrame")
        form.pack(fill="x", padx=8, pady=8)

        pad = {"padx": 16, "pady": 6}

        ttk.Label(form, text="Origin (IATA or city)", style="CardMuted.TLabel").grid(
            row=0, column=0, sticky="w", **pad
        )
        self.origin_var = tk.StringVar(value="HKG")
        ttk.Entry(form, textvariable=self.origin_var, width=28).grid(
            row=1, column=0, sticky="ew", **pad
        )

        ttk.Label(form, text="Destination", style="CardMuted.TLabel").grid(
            row=0, column=1, sticky="w", **pad
        )
        self.dest_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.dest_var, width=28).grid(
            row=1, column=1, sticky="ew", **pad
        )

        ttk.Label(form, text="Depart (YYYY-MM-DD)", style="CardMuted.TLabel").grid(
            row=2, column=0, sticky="w", **pad
        )
        self.depart_var = tk.StringVar(value=_default_depart())
        ttk.Entry(form, textvariable=self.depart_var, width=28).grid(
            row=3, column=0, sticky="ew", **pad
        )

        ttk.Label(form, text="Return (optional)", style="CardMuted.TLabel").grid(
            row=2, column=1, sticky="w", **pad
        )
        self.return_var = tk.StringVar(value=_default_return())
        ttk.Entry(form, textvariable=self.return_var, width=28).grid(
            row=3, column=1, sticky="ew", **pad
        )

        ttk.Label(form, text="Budget (HKD)", style="CardMuted.TLabel").grid(
            row=4, column=0, sticky="w", **pad
        )
        self.budget_var = tk.StringVar(value="12000")
        ttk.Entry(form, textvariable=self.budget_var, width=28).grid(
            row=5, column=0, sticky="ew", **pad
        )

        opts = ttk.Frame(form, style="Card.TFrame")
        opts.grid(row=5, column=1, sticky="w", **pad)
        self.flights_var = tk.BooleanVar(value=True)
        self.trains_var = tk.BooleanVar(value=True)
        self.transfers_var = tk.BooleanVar(value=True)
        self.car_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Flights", variable=self.flights_var).grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        ttk.Checkbutton(opts, text="Trains", variable=self.trains_var).grid(
            row=0, column=1, sticky="w", padx=(0, 10)
        )
        ttk.Checkbutton(opts, text="Transfers", variable=self.transfers_var).grid(
            row=0, column=2, sticky="w", padx=(0, 10)
        )
        ttk.Checkbutton(opts, text="Rental car", variable=self.car_var).grid(
            row=0, column=3, sticky="w"
        )

        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        actions = ttk.Frame(parent, style="TFrame")
        actions.pack(fill="x", padx=8, pady=(0, 8))
        self.plan_btn = ttk.Button(
            actions,
            text="Plan trip",
            style="Accent.TButton",
            command=self._on_plan,
        )
        self.plan_btn.pack(side="left")
        ttk.Button(actions, text="Clear result", command=self._clear_plan_result).pack(
            side="left", padx=10
        )
        ttk.Button(
            actions, text="Open Trip.com", command=lambda: webbrowser.open(
                f"{settings.trip_base_url}/?locale={settings.trip_locale}&curr={settings.trip_currency}"
            )
        ).pack(side="right")

        ttk.Label(parent, text="Plan result", style="Muted.TLabel").pack(
            anchor="w", padx=10
        )
        self.plan_out = scrolledtext.ScrolledText(
            parent,
            wrap="word",
            font=("Consolas", 11),
            bg=COLORS["input_bg"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            padx=12,
            pady=12,
        )
        self.plan_out.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.plan_out.tag_configure("url", foreground=COLORS["accent"], underline=True)
        self.plan_out.bind("<Button-1>", self._on_click_url)

    def _build_chat(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Ask about flight/hotel comparisons or follow-up questions.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self.chat_out = scrolledtext.ScrolledText(
            parent,
            wrap="word",
            font=("Consolas", 11),
            bg=COLORS["input_bg"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            padx=12,
            pady=12,
            state="disabled",
        )
        self.chat_out.pack(fill="both", expand=True, padx=8, pady=4)
        self.chat_out.tag_configure("you", foreground=COLORS["accent"])
        self.chat_out.tag_configure("agent", foreground=COLORS["ok"])
        self.chat_out.tag_configure("sys", foreground=COLORS["muted"])
        self.chat_out.tag_configure("url", foreground=COLORS["accent"], underline=True)
        self.chat_out.bind("<Button-1>", self._on_click_url)

        row = ttk.Frame(parent, style="TFrame")
        row.pack(fill="x", padx=8, pady=8)
        self.chat_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.chat_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda _e: self._on_chat())
        ttk.Button(row, text="Send", style="Accent.TButton", command=self._on_chat).pack(
            side="left"
        )
        ttk.Button(row, text="Reset chat", command=self._reset_chat).pack(
            side="left", padx=(8, 0)
        )

    def _boot_agent(self) -> None:
        def work() -> None:
            try:
                if self.headless:
                    settings.headless = True
                agent = TravelAgent(model=self.model)
                agent.start()
                self.agent = agent
                self.after(0, lambda: self._set_status(
                    f"Ready · model {self.model} · Trip.com HK", ok=True
                ))
            except Exception as exc:
                self.after(
                    0,
                    lambda: self._set_status(f"Browser start failed: {exc}", ok=False),
                )
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "Startup error",
                        f"{exc}\n\nRun: playwright install chromium",
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, text: str, ok: bool | None = None) -> None:
        self.status_var.set(text)
        if ok is True:
            self.status.configure(foreground=COLORS["ok"])
        elif ok is False:
            self.status.configure(foreground=COLORS["danger"])
        else:
            self.status.configure(foreground=COLORS["muted"])

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.plan_btn.configure(state=state)
        if busy:
            self._set_status(label or "Working… searching Trip.com")
        elif self.agent:
            self._set_status(f"Ready · model {self.model} · Trip.com HK", ok=True)

    def _on_plan(self) -> None:
        if self._busy:
            return
        if not self.agent:
            messagebox.showwarning("Not ready", "Browser is still starting.")
            return

        destination = self.dest_var.get().strip()
        depart = self.depart_var.get().strip()
        ret = self.return_var.get().strip() or None
        origin = self.origin_var.get().strip() or "HKG"
        try:
            budget = float(self.budget_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid budget", "Enter a numeric HKD budget.")
            return

        if not destination:
            messagebox.showerror("Missing destination", "Enter a destination.")
            return
        if not depart:
            messagebox.showerror("Missing date", "Enter a depart date (YYYY-MM-DD).")
            return
        if budget <= 0:
            messagebox.showerror("Invalid budget", "Budget must be positive.")
            return

        include_flights = self.flights_var.get()
        include_trains = self.trains_var.get()
        if not include_flights and not include_trains:
            messagebox.showerror(
                "Transport", "Choose at least flights or trains."
            )
            return

        query = build_plan_query(
            destination=destination,
            depart_date=depart,
            return_date=ret,
            budget_hkd=budget,
            origin=origin,
            rent_car=self.car_var.get(),
            include_flights=include_flights,
            include_trains=include_trains,
            include_transfers=self.transfers_var.get(),
        )

        self.plan_out.delete("1.0", "end")
        self.plan_out.insert("end", "Building your plan…\nSearching Trip.com…\n")
        self._set_busy(True, "Planning trip…")

        def work() -> None:
            try:
                assert self.agent is not None
                answer = self.agent.chat(query)
                self.after(0, lambda: self._show_plan(answer))
            except Exception as exc:
                self.after(0, lambda: self._show_plan(f"Error: {exc}"))
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _show_plan(self, text: str) -> None:
        self.plan_out.delete("1.0", "end")
        self._insert_with_urls(self.plan_out, text)

    def _clear_plan_result(self) -> None:
        self.plan_out.delete("1.0", "end")

    def _on_chat(self) -> None:
        if self._busy:
            return
        if not self.agent:
            messagebox.showwarning("Not ready", "Browser is still starting.")
            return
        msg = self.chat_var.get().strip()
        if not msg:
            return
        self.chat_var.set("")
        self._append_chat("You", msg, "you")
        self._set_busy(True, "Thinking…")

        def work() -> None:
            try:
                assert self.agent is not None
                answer = self.agent.chat(msg)
                self.after(0, lambda: self._append_chat("Agent", answer, "agent"))
            except Exception as exc:
                self.after(0, lambda: self._append_chat("Error", str(exc), "sys"))
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _append_chat(self, who: str, text: str, tag: str) -> None:
        self.chat_out.configure(state="normal")
        self.chat_out.insert("end", f"{who}\n", tag)
        self._insert_with_urls(self.chat_out, text + "\n\n")
        self.chat_out.configure(state="disabled")
        self.chat_out.see("end")

    def _reset_chat(self) -> None:
        if self.agent:
            self.agent.reset()
        self.chat_out.configure(state="normal")
        self.chat_out.delete("1.0", "end")
        self.chat_out.configure(state="disabled")
        self._append_chat("System", "Conversation reset.", "sys")

    def _insert_with_urls(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        pos = 0
        for match in _URL_RE.finditer(text):
            if match.start() > pos:
                widget.insert("end", text[pos : match.start()])
            url = match.group(0).rstrip(".,;")
            widget.insert("end", url, ("url", f"link:{url}"))
            pos = match.end()
        if pos < len(text):
            widget.insert("end", text[pos:])

    def _on_click_url(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        widget = event.widget
        index = widget.index(f"@{event.x},{event.y}")
        for tag in widget.tag_names(index):
            if tag.startswith("link:"):
                webbrowser.open(tag[5:])
                break

    def _on_close(self) -> None:
        try:
            if self.agent:
                self.agent.close()
        except Exception:
            pass
        self.destroy()


def _default_depart(days: int = 21) -> str:
    from datetime import timedelta

    return (date.today() + timedelta(days=days)).isoformat()


def _default_return(days: int = 28) -> str:
    from datetime import timedelta

    return (date.today() + timedelta(days=days)).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voyage desktop travel agent")
    parser.add_argument(
        "-m",
        "--model",
        default=settings.ollama_model,
        help=f"Ollama model (default: {settings.ollama_model})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Hide the Trip.com Playwright browser window",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = TravelAgentApp(model=args.model, headless=args.headless)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
