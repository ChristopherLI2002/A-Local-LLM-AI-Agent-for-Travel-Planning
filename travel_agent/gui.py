"""Desktop window UI for the Trip.com + Ollama travel agent (tkinter)."""

from __future__ import annotations

import argparse
import math
import re
import threading
import tkinter as tk
import webbrowser
from datetime import date, timedelta
from tkinter import messagebox, scrolledtext

from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.planner_query import build_plan_query

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Harbor mist — light, coastal, no purple / cream-terracotta / dark-mode defaults
C = {
    "sky_top": "#B9D6E8",
    "sky_mid": "#D7E8F2",
    "sky_bot": "#EEF4F7",
    "paper": "#F7FBFC",
    "ink": "#143246",
    "ink_soft": "#3D5A6C",
    "muted": "#6E8A9A",
    "line": "#C5D7E2",
    "field": "#FFFFFF",
    "field_focus": "#E8F6F4",
    "accent": "#0E8F86",
    "accent_deep": "#0A6E68",
    "accent_glow": "#D5F2EF",
    "chip": "#EAF3F7",
    "chip_on": "#0E8F86",
    "chip_on_text": "#FFFFFF",
    "danger": "#C45B5B",
    "ok": "#2F8F64",
    "shadow": "#9BB4C4",
}

FONT_BRAND = ("Georgia", 36, "bold")
FONT_DISPLAY = ("Georgia", 18)
FONT_UI = ("Segoe UI", 11)
FONT_UI_BOLD = ("Segoe UI Semibold", 11)
FONT_SMALL = ("Segoe UI", 9)
FONT_BODY = ("Segoe UI", 11)
FONT_INPUT = ("Segoe UI", 12)


def _lerp_hex(a: str, b: str, t: float) -> str:
    def parse(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    ar, ag, ab = parse(a)
    br, bg, bb = parse(b)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


class GradientHeader(tk.Canvas):
    """Full-bleed soft sky gradient with brand lockup."""

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, height=148, highlightthickness=0, bd=0, **kwargs)
        self.bind("<Configure>", self._paint)
        self._pulse = 0.0
        self._status = "Starting Trip.com browser…"
        self._status_color = C["muted"]
        self.after(40, self._tick)

    def set_status(self, text: str, color: str | None = None) -> None:
        self._status = text
        if color:
            self._status_color = color
        self._paint()

    def _tick(self) -> None:
        self._pulse = (self._pulse + 0.08) % (math.pi * 2)
        self._paint()
        self.after(50, self._tick)

    def _paint(self, _event: object | None = None) -> None:
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        self.delete("all")
        steps = 48
        for i in range(steps):
            t = i / (steps - 1)
            if t < 0.55:
                color = _lerp_hex(C["sky_top"], C["sky_mid"], t / 0.55)
            else:
                color = _lerp_hex(C["sky_mid"], C["sky_bot"], (t - 0.55) / 0.45)
            y0 = int(h * i / steps)
            y1 = int(h * (i + 1) / steps) + 1
            self.create_rectangle(0, y0, w, y1, outline="", fill=color)

        # Soft horizon haze
        glow = 0.35 + 0.15 * math.sin(self._pulse)
        self.create_oval(
            w * 0.62,
            h * (0.05 - 0.02 * math.sin(self._pulse)),
            w * 0.98,
            h * (0.85 + 0.04 * math.sin(self._pulse)),
            outline="",
            fill=_lerp_hex(C["sky_top"], "#FFFFFF", glow),
        )

        # Thin topographic arcs
        for i, offset in enumerate((18, 34, 50)):
            self.create_arc(
                -80,
                h - 90 + offset,
                w + 80,
                h + 120 + offset,
                start=200,
                extent=140,
                style="arc",
                outline=_lerp_hex(C["line"], C["sky_mid"], 0.4 + i * 0.15),
                width=1,
            )

        self.create_text(
            36,
            42,
            anchor="w",
            text="Voyage",
            fill=C["ink"],
            font=FONT_BRAND,
        )
        self.create_text(
            40,
            88,
            anchor="w",
            text="Your trip starts here — live from Trip.com Hong Kong",
            fill=C["ink_soft"],
            font=FONT_DISPLAY,
        )

        # Status pill
        pad_x, pad_y = 14, 7
        tw = self.create_text(
            36 + pad_x,
            h - 28,
            anchor="w",
            text=self._status,
            fill=self._status_color,
            font=FONT_SMALL,
        )
        bbox = self.bbox(tw)
        if bbox:
            x1, y1, x2, y2 = bbox
            self.create_rectangle(
                x1 - pad_x,
                y1 - pad_y,
                x2 + pad_x,
                y2 + pad_y,
                fill="#FFFFFF",
                outline=C["line"],
                width=1,
            )
            self.tag_raise(tw)


class PillButton(tk.Canvas):
    """Custom primary/secondary button with hover motion."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: object | None = None,
        primary: bool = True,
        width: int = 140,
        height: int = 42,
        **kwargs,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=C["paper"],
            cursor="hand2",
            **kwargs,
        )
        self._text = text
        self._command = command
        self._primary = primary
        self._enabled = True
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def configure(self, **kwargs) -> None:  # type: ignore[override]
        if "state" in kwargs:
            self._enabled = kwargs.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "arrow")
            self._draw()
        if kwargs:
            super().configure(**kwargs)

    def _colors(self) -> tuple[str, str, str]:
        if not self._enabled:
            return C["chip"], C["line"], C["muted"]
        if self._primary:
            fill = C["accent_deep"] if self._hover else C["accent"]
            return fill, fill, "#FFFFFF"
        fill = C["accent_glow"] if self._hover else "#FFFFFF"
        outline = C["accent"] if self._hover else C["line"]
        text = C["accent_deep"] if self._hover else C["ink"]
        return fill, outline, text

    def _draw(self) -> None:
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        fill, outline, fg = self._colors()
        if self._primary and self._enabled and self._hover:
            self.create_rectangle(4, 5, w, h, fill=C["shadow"], outline="")
        # Rounded rectangle via polygon approximation
        r = 12
        points = [
            r, 1,
            w - r - 2, 1,
            w - 2, 1,
            w - 2, r,
            w - 2, h - r - 2,
            w - 2, h - 2,
            w - r - 2, h - 2,
            r, h - 2,
            1, h - 2,
            1, h - r - 2,
            1, r,
            1, 1,
        ]
        self.create_polygon(
            points,
            smooth=True,
            fill=fill,
            outline=outline,
            width=1,
        )
        self.create_text(
            (w - 1) // 2,
            (h - 1) // 2,
            text=self._text,
            fill=fg,
            font=FONT_UI_BOLD,
        )

    def _on_enter(self, _e: object) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _e: object) -> None:
        self._hover = False
        self._draw()

    def _on_click(self, _e: object) -> None:
        if self._enabled and self._command:
            self._command()


class Chip(tk.Label):
    """Toggle chip for transport modes."""

    def __init__(self, master: tk.Misc, text: str, variable: tk.BooleanVar) -> None:
        super().__init__(
            master,
            text=text,
            font=FONT_SMALL,
            padx=12,
            pady=6,
            cursor="hand2",
            bd=0,
        )
        self._var = variable
        self._var.trace_add("write", lambda *_: self._sync())
        self.bind("<Button-1>", self._toggle)
        self._sync()

    def _toggle(self, _e: object) -> None:
        self._var.set(not self._var.get())

    def _sync(self) -> None:
        on = self._var.get()
        self.configure(
            bg=C["chip_on"] if on else C["chip"],
            fg=C["chip_on_text"] if on else C["ink_soft"],
        )


class Field(tk.Frame):
    """Labeled input with focus ring."""

    def __init__(self, master: tk.Misc, label: str, textvariable: tk.StringVar) -> None:
        super().__init__(master, bg=C["paper"])
        tk.Label(
            self,
            text=label.upper(),
            bg=C["paper"],
            fg=C["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        shell = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="x")
        self.entry = tk.Entry(
            shell,
            textvariable=textvariable,
            font=FONT_INPUT,
            bg=C["field"],
            fg=C["ink"],
            insertbackground=C["ink"],
            relief="flat",
            bd=0,
        )
        self.entry.pack(fill="x", ipady=10, ipadx=10)
        self.entry.bind("<FocusIn>", lambda _e: shell.configure(bg=C["accent"]))
        self.entry.bind(
            "<FocusOut>",
            lambda _e: shell.configure(bg=C["line"]),
        )
        self.entry.bind(
            "<FocusIn>",
            lambda _e: self.entry.configure(bg=C["field_focus"]),
            add="+",
        )
        self.entry.bind(
            "<FocusOut>",
            lambda _e: self.entry.configure(bg=C["field"]),
            add="+",
        )


class SegmentedTabs(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        labels: list[str],
        on_change,
    ) -> None:
        super().__init__(master, bg=C["chip"], padx=4, pady=4)
        self._on_change = on_change
        self._btns: list[tk.Label] = []
        self._index = 0
        for i, label in enumerate(labels):
            btn = tk.Label(
                self,
                text=label,
                font=FONT_UI_BOLD,
                padx=18,
                pady=8,
                cursor="hand2",
                bg=C["chip"],
                fg=C["muted"],
            )
            btn.pack(side="left", padx=2)
            btn.bind("<Button-1>", lambda _e, idx=i: self.select(idx))
            self._btns.append(btn)
        self.select(0)

    def select(self, index: int) -> None:
        self._index = index
        for i, btn in enumerate(self._btns):
            active = i == index
            btn.configure(
                bg="#FFFFFF" if active else C["chip"],
                fg=C["ink"] if active else C["muted"],
            )
        self._on_change(index)


class TravelAgentApp(tk.Tk):
    def __init__(self, model: str, headless: bool = False) -> None:
        super().__init__()
        self.model = model
        self.headless = headless
        self.agent: TravelAgent | None = None
        self._busy = False

        self.title("Voyage — Trip.com Travel Agent")
        self.geometry("1040x760")
        self.minsize(900, 640)
        self.configure(bg=C["paper"])
        try:
            self.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

        self.header = GradientHeader(self, bg=C["sky_top"])
        self.header.pack(fill="x")

        body = tk.Frame(self, bg=C["paper"])
        body.pack(fill="both", expand=True, padx=28, pady=(18, 22))

        self.pages = tk.Frame(body, bg=C["paper"])
        self.plan_page = tk.Frame(self.pages, bg=C["paper"])
        self.chat_page = tk.Frame(self.pages, bg=C["paper"])
        self._build_planner(self.plan_page)
        self._build_chat(self.chat_page)

        self.tabs = SegmentedTabs(
            body,
            ["Trip planner", "Chat"],
            on_change=self._show_page,
        )
        self.tabs.pack(anchor="w", pady=(0, 16))
        self.pages.pack(fill="both", expand=True)
        self._show_page(0)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._boot_agent)
        self.after(200, self._intro_motion)

    def _intro_motion(self) -> None:
        # Gentle entrance: nudge form opacity-like via brief highlight
        self.header.set_status("Warming up…", C["muted"])

    def _sync_return_from_depart(self, *_args: object) -> None:
        """Keep default trip length at 1 week when depart date changes."""
        raw = self.depart_var.get().strip()
        try:
            depart = date.fromisoformat(raw)
        except ValueError:
            return
        self.return_var.set((depart + timedelta(days=7)).isoformat())

    def _show_page(self, index: int) -> None:
        if not hasattr(self, "plan_page") or not hasattr(self, "chat_page"):
            return
        self.plan_page.pack_forget()
        self.chat_page.pack_forget()
        page = self.plan_page if index == 0 else self.chat_page
        page.pack(fill="both", expand=True)

    def _build_planner(self, parent: tk.Frame) -> None:
        # Two-column composition: form left, result right on wide screens
        split = tk.Frame(parent, bg=C["paper"])
        split.pack(fill="both", expand=True)
        split.columnconfigure(0, weight=2, minsize=320)
        split.columnconfigure(1, weight=3, minsize=360)
        split.rowconfigure(0, weight=1)

        left = tk.Frame(split, bg=C["paper"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right = tk.Frame(split, bg=C["paper"])
        right.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            left,
            text="Where next?",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_DISPLAY,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        tk.Label(
            left,
            text="Set route, dates, and budget. Voyage searches Trip.com for you.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
            wraplength=360,
            justify="left",
        ).pack(fill="x", pady=(0, 14))

        grid = tk.Frame(left, bg=C["paper"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.origin_var = tk.StringVar(value="Hong Kong")
        self.dest_var = tk.StringVar()
        self.depart_var = tk.StringVar(value=_default_depart())
        self.return_var = tk.StringVar(value=_default_return())
        self.budget_var = tk.StringVar(value="12000")
        self.depart_var.trace_add("write", self._sync_return_from_depart)

        Field(grid, "Origin (city)", self.origin_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 12)
        )
        Field(grid, "Destination (city)", self.dest_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 12)
        )
        Field(grid, "Depart (YYYY-MM-DD)", self.depart_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 12)
        )
        Field(grid, "Return (1 week trip)", self.return_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 12)
        )
        Field(grid, "Budget (HKD)", self.budget_var).grid(
            row=2, column=0, sticky="ew", padx=(0, 8), pady=(0, 12)
        )

        tk.Label(
            left,
            text="TRANSPORT",
            bg=C["paper"],
            fg=C["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(4, 6))

        chips = tk.Frame(left, bg=C["paper"])
        chips.pack(fill="x", pady=(0, 16))
        self.flights_var = tk.BooleanVar(value=True)
        self.trains_var = tk.BooleanVar(value=True)
        self.transfers_var = tk.BooleanVar(value=True)
        self.car_var = tk.BooleanVar(value=False)
        for text, var in (
            ("Flights", self.flights_var),
            ("Trains", self.trains_var),
            ("Transfers", self.transfers_var),
            ("Rental car", self.car_var),
        ):
            Chip(chips, text, var).pack(side="left", padx=(0, 8))

        actions = tk.Frame(left, bg=C["paper"])
        actions.pack(fill="x", pady=(4, 0))
        self.plan_btn = PillButton(
            actions, "Plan trip", command=self._on_plan, primary=True, width=150
        )
        self.plan_btn.pack(side="left")
        PillButton(
            actions,
            "Clear",
            command=self._clear_plan_result,
            primary=False,
            width=100,
        ).pack(side="left", padx=10)
        PillButton(
            actions,
            "Trip.com",
            command=lambda: webbrowser.open(
                f"{settings.trip_base_url}/?locale={settings.trip_locale}"
                f"&curr={settings.trip_currency}"
            ),
            primary=False,
            width=110,
        ).pack(side="right")

        # Result panel
        tk.Label(
            right,
            text="Your plan",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_DISPLAY,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        result_shell = tk.Frame(right, bg=C["line"], padx=1, pady=1)
        result_shell.pack(fill="both", expand=True)
        self.plan_out = scrolledtext.ScrolledText(
            result_shell,
            wrap="word",
            font=FONT_BODY,
            bg=C["field"],
            fg=C["ink"],
            insertbackground=C["ink"],
            relief="flat",
            bd=0,
            padx=18,
            pady=16,
            spacing1=2,
            spacing3=4,
        )
        self.plan_out.pack(fill="both", expand=True)
        self.plan_out.insert(
            "end",
            "Fill in your trip details and press Plan trip.\n"
            "Live fares and hotels will appear here.",
        )
        self.plan_out.tag_configure("url", foreground=C["accent"], underline=True)
        self.plan_out.tag_configure("placeholder", foreground=C["muted"])
        self.plan_out.tag_add("placeholder", "1.0", "end")
        self.plan_out.bind("<Button-1>", self._on_click_url)

    def _build_chat(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text="Ask Voyage",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_DISPLAY,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        tk.Label(
            parent,
            text="Compare flights, hotels, or refine a plan in plain language.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
        ).pack(fill="x", pady=(0, 12))

        shell = tk.Frame(parent, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="both", expand=True)
        self.chat_out = scrolledtext.ScrolledText(
            shell,
            wrap="word",
            font=FONT_BODY,
            bg=C["field"],
            fg=C["ink"],
            insertbackground=C["ink"],
            relief="flat",
            bd=0,
            padx=18,
            pady=16,
            state="disabled",
        )
        self.chat_out.pack(fill="both", expand=True)
        self.chat_out.tag_configure("you", foreground=C["accent_deep"], font=FONT_UI_BOLD)
        self.chat_out.tag_configure("agent", foreground=C["ok"], font=FONT_UI_BOLD)
        self.chat_out.tag_configure("sys", foreground=C["muted"], font=FONT_UI)
        self.chat_out.tag_configure("url", foreground=C["accent"], underline=True)
        self.chat_out.bind("<Button-1>", self._on_click_url)

        row = tk.Frame(parent, bg=C["paper"])
        row.pack(fill="x", pady=(12, 0))
        self.chat_var = tk.StringVar()
        field_shell = tk.Frame(row, bg=C["line"], padx=1, pady=1)
        field_shell.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry = tk.Entry(
            field_shell,
            textvariable=self.chat_var,
            font=FONT_INPUT,
            bg=C["field"],
            fg=C["ink"],
            insertbackground=C["ink"],
            relief="flat",
            bd=0,
        )
        entry.pack(fill="x", ipady=11, ipadx=12)
        entry.bind("<Return>", lambda _e: self._on_chat())
        entry.bind("<FocusIn>", lambda _e: field_shell.configure(bg=C["accent"]))
        entry.bind("<FocusOut>", lambda _e: field_shell.configure(bg=C["line"]))

        PillButton(row, "Send", command=self._on_chat, primary=True, width=110).pack(
            side="left"
        )
        PillButton(
            row, "Reset", command=self._reset_chat, primary=False, width=100
        ).pack(side="left", padx=(8, 0))

    def _boot_agent(self) -> None:
        def work() -> None:
            try:
                if self.headless:
                    settings.headless = True
                agent = TravelAgent(model=self.model)
                agent.start()
                self.agent = agent
                self.after(
                    0,
                    lambda: self._set_status(
                        f"Ready · {self.model} · Trip.com HK", C["ok"]
                    ),
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda: self._set_status(f"Browser failed: {exc}", C["danger"]),
                )
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "Startup error",
                        f"{exc}\n\nRun: playwright install chromium",
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.header.set_status(text, color or C["muted"])

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self.plan_btn.configure(state="disabled" if busy else "normal")
        if busy:
            self._set_status(label or "Searching Trip.com…", C["accent_deep"])
        elif self.agent:
            self._set_status(f"Ready · {self.model} · Trip.com HK", C["ok"])

    def _on_plan(self) -> None:
        if self._busy:
            return
        if not self.agent:
            messagebox.showwarning("Not ready", "Browser is still starting.")
            return

        destination = self.dest_var.get().strip()
        depart = self.depart_var.get().strip()
        ret = self.return_var.get().strip() or None
        origin = self.origin_var.get().strip() or "Hong Kong"
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
            messagebox.showerror("Transport", "Choose at least flights or trains.")
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

        # Update status text while long comparisons run
        self.plan_out.delete("1.0", "end")
        self.plan_out.insert(
            "end",
            "Building your plan…\n"
            "Comparing flights and hotels on Trip.com (this can take a few minutes)…\n",
        )
        self._set_busy(True, "Comparing flights & hotels…")

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
        self.plan_out.insert(
            "end",
            "Fill in your trip details and press Plan trip.\n"
            "Live fares and hotels will appear here.",
        )
        self.plan_out.tag_add("placeholder", "1.0", "end")

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


def _default_depart() -> str:
    """Always tomorrow."""
    return (date.today() + timedelta(days=1)).isoformat()


def _default_return() -> str:
    """One-week trip: return 7 days after tomorrow (8 days from today)."""
    return (date.today() + timedelta(days=8)).isoformat()


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
