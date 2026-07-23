"""Trip.Planner-style desktop UI for the Voyage travel agent (tkinter)."""

from __future__ import annotations

import argparse
import math
import queue
import re
import threading
import tkinter as tk
import urllib.request
import webbrowser
from collections.abc import Callable
from datetime import date, timedelta
from io import BytesIO
from typing import Any
from tkinter import messagebox, scrolledtext

from PIL import Image, ImageTk

from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.airline_names import airline_logo_url, is_plausible_airline_name
from travel_agent.itinerary_parse import (
    FlightOffer,
    HotelOffer,
    ParsedItinerary,
    ensure_day_blocks,
    parse_itinerary,
)
from travel_agent.places import to_flight_code, to_hotel_city
from travel_agent.planner_query import TRAVEL_STYLES, build_plan_query
from travel_agent.trip_urls import (
    build_flight_search_url,
    build_hotel_list_url,
    canonicalize_hotel_detail_url,
    fetch_flight_card,
    fetch_hotel_detail_link,
    is_openable_hotel_url,
    is_trusted_hotel_detail_url,
    resolve_booking_url,
)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

TRIP_PLANNER_URL = (
    "https://hk.trip.com/webapp/tripmap/tripplanner"
    "?source=t_online_homepage&locale=en-HK&curr=HKD"
)

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
    "card": "#FFFFFF",
    "trip_blue": "#3264FF",
    "trip_blue_hover": "#254ED6",
    "badge_teal": "#0B7A74",
    "badge_outline": "#5BB8B1",
}

FONT_SCALE_STEPS = 13  # discrete positions 0..12 (matches A——A slider)
DEFAULT_FONT_STEP = 5  # bigger default than the old ~11pt UI


def _font_factor(step: int) -> float:
    """Map slider step to size multiplier (1.0 at DEFAULT_FONT_STEP)."""
    step = max(0, min(FONT_SCALE_STEPS - 1, int(step)))
    # step 0 ≈ 0.78x, default ≈ 1.0x, max ≈ 1.35x relative to bigger baseline
    return 0.78 + step * ((1.35 - 0.78) / (FONT_SCALE_STEPS - 1))


def _scaled_pt(base: int, step: int) -> int:
    return max(8, round(base * _font_factor(step)))


def make_fonts(step: int = DEFAULT_FONT_STEP) -> dict[str, tuple]:
    """Build the app font set for a given slider step."""
    # Baselines are already larger than the original Voyage defaults
    return {
        "brand": ("Georgia", _scaled_pt(36, step), "bold"),
        "display": ("Georgia", _scaled_pt(20, step)),
        "ui": ("Segoe UI", _scaled_pt(13, step)),
        "ui_bold": ("Segoe UI Semibold", _scaled_pt(13, step)),
        "small": ("Segoe UI", _scaled_pt(11, step)),
        "body": ("Segoe UI", _scaled_pt(13, step)),
        "input": ("Segoe UI", _scaled_pt(14, step)),
        "day_num": ("Segoe UI", _scaled_pt(38, step), "bold"),
        "day_badge": ("Segoe UI Semibold", _scaled_pt(9, step)),
        "time": ("Segoe UI Semibold", _scaled_pt(16, step)),
        "price": ("Segoe UI Semibold", _scaled_pt(17, step)),
        "hotel_name": ("Segoe UI Semibold", _scaled_pt(14, step)),
        "tiny": ("Segoe UI", _scaled_pt(9, step)),
        "wizard": ("Georgia", _scaled_pt(28, step)),
        "label": ("Segoe UI", _scaled_pt(9, step)),
    }


# Mutable current fonts (widgets read these at create/refresh time)
FONTS = make_fonts(DEFAULT_FONT_STEP)
FONT_BRAND = FONTS["brand"]
FONT_DISPLAY = FONTS["display"]
FONT_UI = FONTS["ui"]
FONT_UI_BOLD = FONTS["ui_bold"]
FONT_SMALL = FONTS["small"]
FONT_BODY = FONTS["body"]
FONT_INPUT = FONTS["input"]
FONT_DAY_NUM = FONTS["day_num"]
FONT_DAY_BADGE = FONTS["day_badge"]


def apply_font_globals(step: int) -> None:
    """Refresh module-level FONT_* aliases used across widgets."""
    global FONT_BRAND, FONT_DISPLAY, FONT_UI, FONT_UI_BOLD, FONT_SMALL
    global FONT_BODY, FONT_INPUT, FONT_DAY_NUM, FONT_DAY_BADGE, FONTS
    FONTS = make_fonts(step)
    FONT_BRAND = FONTS["brand"]
    FONT_DISPLAY = FONTS["display"]
    FONT_UI = FONTS["ui"]
    FONT_UI_BOLD = FONTS["ui_bold"]
    FONT_SMALL = FONTS["small"]
    FONT_BODY = FONTS["body"]
    FONT_INPUT = FONTS["input"]
    FONT_DAY_NUM = FONTS["day_num"]
    FONT_DAY_BADGE = FONTS["day_badge"]


# Seoul-style day card accents (cycles per day)
_DAY_ACCENTS = (
    "#E85A4F",  # coral
    "#F0A202",  # amber
    "#3CB371",  # mint
    "#3D9BE9",  # sky
    "#E056A0",  # magenta
    "#7B6CF6",  # violet
)


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


def _default_depart() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def _default_return(nights: int = 7) -> str:
    return (date.today() + timedelta(days=1 + nights)).isoformat()


class GradientHeader(tk.Canvas):
    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, height=132, highlightthickness=0, bd=0, **kwargs)
        self.bind("<Configure>", self._paint)
        self._pulse = 0.0
        self._status = "Starting Trip.com browser…"
        self._status_color = C["muted"]
        self.after(50, self._tick)

    def set_status(self, text: str, color: str | None = None) -> None:
        self._status = text
        if color:
            self._status_color = color
        self._paint()

    def _tick(self) -> None:
        self._pulse = (self._pulse + 0.07) % (math.pi * 2)
        self._paint()
        self.after(60, self._tick)

    def _paint(self, _event: object | None = None) -> None:
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        self.delete("all")
        steps = 40
        for i in range(steps):
            t = i / (steps - 1)
            color = (
                _lerp_hex(C["sky_top"], C["sky_mid"], t / 0.55)
                if t < 0.55
                else _lerp_hex(C["sky_mid"], C["sky_bot"], (t - 0.55) / 0.45)
            )
            y0 = int(h * i / steps)
            y1 = int(h * (i + 1) / steps) + 1
            self.create_rectangle(0, y0, w, y1, outline="", fill=color)

        glow = 0.35 + 0.12 * math.sin(self._pulse)
        self.create_oval(
            w * 0.58,
            -10,
            w * 1.05,
            h * 0.95,
            outline="",
            fill=_lerp_hex(C["sky_top"], "#FFFFFF", glow),
        )
        self.create_text(36, 38, anchor="w", text="Voyage", fill=C["ink"], font=FONT_BRAND)
        self.create_text(
            40,
            78,
            anchor="w",
            text="Trip.Planner-style itineraries · live from Trip.com Hong Kong",
            fill=C["ink_soft"],
            font=FONT_DISPLAY,
        )
        tw = self.create_text(
            50,
            h - 22,
            anchor="w",
            text=self._status,
            fill=self._status_color,
            font=FONT_SMALL,
        )
        bbox = self.bbox(tw)
        if bbox:
            x1, y1, x2, y2 = bbox
            self.create_rectangle(
                x1 - 12, y1 - 6, x2 + 12, y2 + 6, fill="#FFFFFF", outline=C["line"]
            )
            self.tag_raise(tw)


class FontSizeSlider(tk.Canvas):
    """Discrete A——A font slider (small A left, large A right)."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        steps: int = FONT_SCALE_STEPS,
        value: int = DEFAULT_FONT_STEP,
        command: Callable[[int], None] | None = None,
        width: int = 220,
        height: int = 36,
        **kwargs,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=C["paper"],
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            **kwargs,
        )
        self._steps = max(2, int(steps))
        self._value = max(0, min(self._steps - 1, int(value)))
        self._command = command
        self._dragging = False
        self.bind("<Configure>", lambda _e: self._paint())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after(10, self._paint)

    @property
    def value(self) -> int:
        return self._value

    def set_value(self, step: int, *, notify: bool = False) -> None:
        step = max(0, min(self._steps - 1, int(step)))
        if step == self._value and not notify:
            self._paint()
            return
        self._value = step
        self._paint()
        if notify and self._command:
            self._command(self._value)

    def _track_geom(self) -> tuple[float, float, float, float]:
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        left = 28.0
        right = w - 28.0
        y = h / 2
        return left, right, y, w

    def _step_from_x(self, x: float) -> int:
        left, right, _y, _w = self._track_geom()
        if right <= left:
            return 0
        t = (x - left) / (right - left)
        t = max(0.0, min(1.0, t))
        return int(round(t * (self._steps - 1)))

    def _on_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._dragging = True
        self.set_value(self._step_from_x(event.x), notify=True)

    def _on_drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._dragging:
            self.set_value(self._step_from_x(event.x), notify=True)

    def _on_release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._dragging = False

    def _paint(self, _event: object | None = None) -> None:
        self.delete("all")
        left, right, y, w = self._track_geom()
        h = max(self.winfo_height(), 2)
        # End labels: small A / large A
        self.create_text(
            12,
            y,
            text="A",
            fill="#A8B4BE",
            font=("Segoe UI", 10),
        )
        self.create_text(
            w - 12,
            y,
            text="A",
            fill="#A8B4BE",
            font=("Segoe UI", 18, "bold"),
        )
        # Track
        self.create_line(left, y, right, y, fill="#6E7A84", width=2, capstyle=tk.ROUND)
        # Step dots
        for i in range(self._steps):
            t = i / (self._steps - 1)
            x = left + (right - left) * t
            r = 3.2
            self.create_oval(x - r, y - r, x + r, y + r, fill="#6E7A84", outline="")
        # Thumb
        t = self._value / (self._steps - 1)
        x = left + (right - left) * t
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill="#FFFFFF", outline="#5A6570", width=1)
        self.create_oval(x - 5.5, y - 5.5, x + 5.5, y + 5.5, fill="#FFFFFF", outline="")


class PillButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: object | None = None,
        primary: bool = True,
        width: int = 150,
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
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self._draw()

    def configure(self, **kwargs) -> None:  # type: ignore[override]
        if "state" in kwargs:
            self._enabled = kwargs.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "arrow")
            self._draw()
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            self._draw()
        if kwargs:
            super().configure(**kwargs)

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self._draw()

    def _colors(self) -> tuple[str, str, str]:
        if not self._enabled:
            return C["chip"], C["line"], C["muted"]
        if self._primary:
            fill = C["accent_deep"] if self._hover else C["accent"]
            return fill, fill, "#FFFFFF"
        fill = C["accent_glow"] if self._hover else "#FFFFFF"
        return fill, C["accent"] if self._hover else C["line"], C["ink"]

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        fill, outline, fg = self._colors()
        r = 12
        points = [
            r, 1, w - r - 2, 1, w - 2, 1, w - 2, r,
            w - 2, h - r - 2, w - 2, h - 2, w - r - 2, h - 2,
            r, h - 2, 1, h - 2, 1, h - r - 2, 1, r, 1, 1,
        ]
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=1)
        self.create_text((w - 1) // 2, (h - 1) // 2, text=self._text, fill=fg, font=FONT_UI_BOLD)

    def _click(self, _e: object) -> None:
        if self._enabled and callable(self._command):
            self._command()


class Chip(tk.Label):
    def __init__(self, master: tk.Misc, text: str, variable: tk.BooleanVar) -> None:
        super().__init__(master, text=text, font=FONT_SMALL, padx=12, pady=7, cursor="hand2", bd=0)
        self._var = variable
        self._var.trace_add("write", lambda *_: self._sync())
        self.bind("<Button-1>", lambda _e: self._var.set(not self._var.get()))
        self._sync()

    def _sync(self) -> None:
        on = self._var.get()
        self.configure(
            bg=C["chip_on"] if on else C["chip"],
            fg=C["chip_on_text"] if on else C["ink_soft"],
        )


class Field(tk.Frame):
    def __init__(self, master: tk.Misc, label: str, textvariable: tk.StringVar) -> None:
        super().__init__(master, bg=C["paper"])
        tk.Label(
            self, text=label.upper(), bg=C["paper"], fg=C["muted"], font=FONTS["tiny"], anchor="w"
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
        self.entry.bind("<FocusOut>", lambda _e: shell.configure(bg=C["line"]))
        self.entry.bind("<FocusIn>", lambda _e: self.entry.configure(bg=C["field_focus"]), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self.entry.configure(bg=C["field"]), add="+")


class LinkLabel(tk.Label):
    def __init__(self, master: tk.Misc, url: str, **kwargs) -> None:
        super().__init__(
            master,
            text=url,
            fg=C["accent"],
            bg=kwargs.pop("bg", C["card"]),
            font=FONT_SMALL,
            cursor="hand2",
            wraplength=kwargs.pop("wraplength", 320),
            justify="left",
            anchor="w",
            **kwargs,
        )
        self._url = url
        self.bind("<Button-1>", lambda _e: webbrowser.open(self._url))


class FlightRowCard(tk.Frame):
    """Trip.com-style flight result row (badges, times, duration, price, Select)."""

    _LOGO_SIZE = 48

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, bg=C["paper"], **kwargs)
        tk.Label(
            self,
            text="Recommended flight",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        shell = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="x")
        self.card = tk.Frame(shell, bg="#FFFFFF", padx=14, pady=12)
        self.card.pack(fill="x")

        self.badges = tk.Frame(self.card, bg="#FFFFFF")
        self.badges.pack(fill="x", pady=(0, 8))

        times = tk.Frame(self.card, bg="#FFFFFF")
        times.pack(fill="x")

        # --- Outbound leg ---
        self.leg_out_lbl = tk.Label(
            times, text="Outbound", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL, anchor="w"
        )
        self.leg_out_lbl.pack(fill="x", pady=(0, 4))

        out_air = tk.Frame(times, bg="#FFFFFF")
        out_air.pack(fill="x", pady=(0, 6))
        sz = self._LOGO_SIZE
        self.logo = tk.Canvas(out_air, width=sz, height=sz, bg="#FFFFFF", highlightthickness=0)
        self.logo.pack(side="left", padx=(0, 10))
        self._logo_photo: tk.PhotoImage | None = None
        self.airline_lbl = tk.Label(
            out_air, text="—", bg="#FFFFFF", fg=C["ink"], font=FONT_UI, anchor="w"
        )
        self.airline_lbl.pack(side="left", fill="x", expand=True)

        out_row = tk.Frame(times, bg="#FFFFFF")
        out_row.pack(fill="x")
        out_row.columnconfigure(0, weight=1)
        out_row.columnconfigure(1, weight=2)
        out_row.columnconfigure(2, weight=1)

        dep = tk.Frame(out_row, bg="#FFFFFF")
        dep.grid(row=0, column=0, sticky="w")
        self.dep_time = tk.Label(
            dep, text="--:--", bg="#FFFFFF", fg="#111111", font=FONTS["time"]
        )
        self.dep_time.pack(anchor="w")
        self.dep_airport = tk.Label(
            dep, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.dep_airport.pack(anchor="w")

        mid = tk.Frame(out_row, bg="#FFFFFF")
        mid.grid(row=0, column=1, sticky="ew", padx=6)
        self.duration = tk.Label(
            mid, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.duration.pack()
        self.path = tk.Canvas(mid, height=14, bg="#FFFFFF", highlightthickness=0)
        self.path.pack(fill="x", pady=2)
        self.stops = tk.Label(
            mid, text="Direct", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.stops.pack()
        self.path.bind("<Configure>", lambda _e: self._draw_path())

        arr = tk.Frame(out_row, bg="#FFFFFF")
        arr.grid(row=0, column=2, sticky="e")
        self.arr_time = tk.Label(
            arr, text="--:--", bg="#FFFFFF", fg="#111111", font=FONTS["time"]
        )
        self.arr_time.pack(anchor="e")
        self.arr_airport = tk.Label(
            arr, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.arr_airport.pack(anchor="e")

        # --- Return leg ---
        self.return_block = tk.Frame(times, bg="#FFFFFF")
        self.return_block.pack(fill="x", pady=(12, 0))
        self.leg_ret_lbl = tk.Label(
            self.return_block,
            text="Return",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONT_SMALL,
            anchor="w",
        )
        self.leg_ret_lbl.pack(fill="x", pady=(0, 4))

        ret_air = tk.Frame(self.return_block, bg="#FFFFFF")
        ret_air.pack(fill="x", pady=(0, 6))
        self.ret_logo = tk.Canvas(
            ret_air, width=sz, height=sz, bg="#FFFFFF", highlightthickness=0
        )
        self.ret_logo.pack(side="left", padx=(0, 10))
        self._ret_logo_photo: tk.PhotoImage | None = None
        self.return_airline_lbl = tk.Label(
            ret_air,
            text="—",
            bg="#FFFFFF",
            fg=C["ink"],
            font=FONT_UI,
            anchor="w",
        )
        self.return_airline_lbl.pack(side="left", fill="x", expand=True)

        ret_row = tk.Frame(self.return_block, bg="#FFFFFF")
        ret_row.pack(fill="x")
        ret_row.columnconfigure(0, weight=1)
        ret_row.columnconfigure(1, weight=2)
        ret_row.columnconfigure(2, weight=1)

        rdep = tk.Frame(ret_row, bg="#FFFFFF")
        rdep.grid(row=0, column=0, sticky="w")
        self.ret_dep_time = tk.Label(
            rdep, text="--:--", bg="#FFFFFF", fg="#111111", font=FONTS["time"]
        )
        self.ret_dep_time.pack(anchor="w")
        self.ret_dep_airport = tk.Label(
            rdep, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.ret_dep_airport.pack(anchor="w")

        rmid = tk.Frame(ret_row, bg="#FFFFFF")
        rmid.grid(row=0, column=1, sticky="ew", padx=6)
        self.ret_duration = tk.Label(
            rmid, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.ret_duration.pack()
        self.ret_path = tk.Canvas(rmid, height=14, bg="#FFFFFF", highlightthickness=0)
        self.ret_path.pack(fill="x", pady=2)
        self.ret_stops = tk.Label(
            rmid, text="Direct", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.ret_stops.pack()
        self.ret_path.bind("<Configure>", lambda _e: self._draw_ret_path())

        rarr = tk.Frame(ret_row, bg="#FFFFFF")
        rarr.grid(row=0, column=2, sticky="e")
        self.ret_arr_time = tk.Label(
            rarr, text="--:--", bg="#FFFFFF", fg="#111111", font=FONTS["time"]
        )
        self.ret_arr_time.pack(anchor="e")
        self.ret_arr_airport = tk.Label(
            rarr, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.ret_arr_airport.pack(anchor="e")

        bottom = tk.Frame(self.card, bg="#FFFFFF")
        bottom.pack(fill="x", pady=(10, 0))
        price_col = tk.Frame(bottom, bg="#FFFFFF")
        price_col.pack(side="left")
        self.price = tk.Label(
            price_col,
            text="—",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=FONTS["price"],
        )
        self.price.pack(anchor="w")
        self.trip_lbl = tk.Label(
            price_col, text="Return", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.trip_lbl.pack(anchor="w")
        self.select_btn = tk.Label(
            bottom,
            text="Select",
            bg=C["trip_blue"],
            fg="#FFFFFF",
            font=FONT_UI_BOLD,
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.select_btn.pack(side="right")
        self._url = ""
        self.select_btn.bind("<Button-1>", self._open)
        self.select_btn.bind(
            "<Enter>", lambda _e: self.select_btn.configure(bg=C["trip_blue_hover"])
        )
        self.select_btn.bind(
            "<Leave>", lambda _e: self.select_btn.configure(bg=C["trip_blue"])
        )

        self.set_loading("Waiting for plan…")

    @staticmethod
    def _format_flight_date(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        try:
            return date.fromisoformat(text).strftime("%a, %d %b %Y")
        except ValueError:
            return text

    def _draw_path(self) -> None:
        self.path.delete("all")
        w = max(self.path.winfo_width(), 40)
        y = 7
        self.path.create_line(8, y, w - 8, y, fill="#C5CDD6", width=2)
        self.path.create_oval(4, y - 3, 10, y + 3, fill="#C5CDD6", outline="")
        self.path.create_oval(w - 10, y - 3, w - 4, y + 3, fill="#C5CDD6", outline="")

    def _draw_ret_path(self) -> None:
        self.ret_path.delete("all")
        w = max(self.ret_path.winfo_width(), 40)
        y = 7
        self.ret_path.create_line(8, y, w - 8, y, fill="#C5CDD6", width=2)
        self.ret_path.create_oval(4, y - 3, 10, y + 3, fill="#C5CDD6", outline="")
        self.ret_path.create_oval(w - 10, y - 3, w - 4, y + 3, fill="#C5CDD6", outline="")

    def _draw_logo_on(
        self,
        canvas: tk.Canvas,
        *,
        initials: str,
        photo_attr: str,
    ) -> None:
        setattr(self, photo_attr, None)
        canvas.delete("all")
        sz = self._LOGO_SIZE
        cx = sz // 2
        # Soft rounded square (not the old triangle placeholder)
        pad = max(2, sz // 14)
        canvas.create_oval(pad, pad, sz - pad, sz - pad, fill="#EEF3F8", outline="#D5DEE8")
        canvas.create_text(
            cx,
            cx,
            text=(initials or "TP")[:2].upper(),
            fill=C["badge_teal"],
            font=("Segoe UI", max(9, sz // 4), "bold"),
        )

    def _draw_logo(self, initials: str) -> None:
        self._draw_logo_on(self.logo, initials=initials, photo_attr="_logo_photo")

    def _draw_ret_logo(self, initials: str) -> None:
        self._draw_logo_on(self.ret_logo, initials=initials, photo_attr="_ret_logo_photo")

    def _fetch_logo_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://hk.trip.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        return urllib.request.urlopen(req, timeout=10).read()

    def _load_logo_photo(
        self,
        *,
        airline: str,
        logo_url: str,
        canvas: tk.Canvas,
        photo_attr: str,
    ) -> None:
        initials = "".join(w[0] for w in airline.split()[:3] if w) or "TP"
        # Prefer Trip.com CDN by IATA code — scraped URLs are often tiny/broken
        cdn = airline_logo_url(airline).strip()
        candidates = [u for u in (cdn, (logo_url or "").strip()) if u.startswith("http")]
        # Dedupe while preserving order
        seen: set[str] = set()
        urls: list[str] = []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if not urls:
            self._draw_logo_on(canvas, initials=initials, photo_attr=photo_attr)
            return
        last_err: Exception | None = None
        for url in urls:
            try:
                data = self._fetch_logo_bytes(url)
                if len(data) < 200:
                    continue
                target = self._LOGO_SIZE
                img = Image.open(BytesIO(data)).convert("RGBA")
                # Fit inside square with padding so logos aren't clipped
                pad = max(2, target // 10)
                box = target - pad * 2
                scale = min(box / max(img.width, 1), box / max(img.height, 1))
                new_w = max(1, round(img.width * scale))
                new_h = max(1, round(img.height * scale))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                canvas_img = Image.new("RGBA", (target, target), (255, 255, 255, 0))
                canvas_img.paste(
                    resized,
                    ((target - new_w) // 2, (target - new_h) // 2),
                    resized,
                )
                photo = ImageTk.PhotoImage(canvas_img)
                setattr(self, photo_attr, photo)
                canvas.delete("all")
                cx = target // 2
                canvas.create_image(cx, cx, image=photo)
                return
            except Exception as exc:
                last_err = exc
                continue
        _ = last_err
        self._draw_logo_on(canvas, initials=initials, photo_attr=photo_attr)

    def _set_airline_logo(self, offer: FlightOffer) -> None:
        self._load_logo_photo(
            airline=offer.airline,
            logo_url=offer.airline_logo,
            canvas=self.logo,
            photo_attr="_logo_photo",
        )

    def _set_return_airline_logo(self, offer: FlightOffer) -> None:
        airline = offer.return_airline or offer.airline
        self._load_logo_photo(
            airline=airline,
            logo_url=offer.return_airline_logo or (offer.airline_logo if not offer.return_airline else ""),
            canvas=self.ret_logo,
            photo_attr="_ret_logo_photo",
        )

    def _open(self, _e: object | None = None) -> None:
        if self._url and "trip.com" in self._url.lower():
            webbrowser.open(self._url)
        elif self._url:
            messagebox.showwarning("Invalid link", "No valid Trip.com booking link is available yet.")
        else:
            messagebox.showinfo("No link", "Generate an itinerary first to get a booking link.")

    def set_loading(self, message: str) -> None:
        self._clear_badges()
        self._add_badge(message, filled=False)
        self.leg_out_lbl.configure(text="Outbound")
        self.airline_lbl.configure(text="Searching Trip.com…")
        self._draw_logo("…")
        self.dep_time.configure(text="--:--")
        self.arr_time.configure(text="--:--")
        self.dep_airport.configure(text="—")
        self.arr_airport.configure(text="—")
        self.duration.configure(text="—")
        self.stops.configure(text="—")
        self.leg_ret_lbl.configure(text="Return")
        self.return_airline_lbl.configure(text="—")
        self._draw_ret_logo("…")
        self.ret_dep_time.configure(text="--:--")
        self.ret_arr_time.configure(text="--:--")
        self.ret_dep_airport.configure(text="—")
        self.ret_arr_airport.configure(text="—")
        self.ret_duration.configure(text="—")
        self.ret_stops.configure(text="—")
        self.return_block.pack_forget()
        self.price.configure(text="…")
        self.trip_lbl.configure(text="")
        self._url = ""

    def _clear_badges(self) -> None:
        for child in self.badges.winfo_children():
            child.destroy()

    def _add_badge(self, text: str, filled: bool = True) -> None:
        if not text:
            return
        if filled:
            tk.Label(
                self.badges,
                text=text,
                bg=C["badge_teal"],
                fg="#FFFFFF",
                font=("Segoe UI", 8, "bold"),
                padx=8,
                pady=3,
            ).pack(side="left", padx=(0, 6))
        else:
            outer = tk.Frame(self.badges, bg=C["badge_outline"], padx=1, pady=1)
            outer.pack(side="left", padx=(0, 6))
            tk.Label(
                outer,
                text=text,
                bg="#FFFFFF",
                fg=C["badge_teal"],
                font=FONTS["tiny"],
                padx=7,
                pady=2,
            ).pack()

    def set_offer(self, offer: FlightOffer) -> None:
        self._clear_badges()
        self._add_badge(offer.badge or "Recommended", filled=True)
        if offer.baggage:
            self._add_badge(offer.baggage, filled=False)

        out_date = self._format_flight_date(offer.depart_date)
        self.leg_out_lbl.configure(
            text=f"Outbound · {out_date}" if out_date else "Outbound"
        )
        self._set_airline_logo(offer)
        self.airline_lbl.configure(text=offer.airline)
        self.dep_time.configure(text=offer.depart_time)
        self.arr_time.configure(text=offer.arrive_time)
        self.dep_airport.configure(text=offer.depart_airport)
        self.arr_airport.configure(text=offer.arrive_airport)
        self.duration.configure(text=offer.duration)
        self.stops.configure(text=offer.stops)
        self.price.configure(text=offer.price_label)
        self.trip_lbl.configure(text=offer.trip_label)
        self._url = offer.url
        self.after(30, self._draw_path)

        has_return = bool(offer.return_depart_time and offer.return_depart_time != "--:--")
        if has_return:
            if not self.return_block.winfo_ismapped():
                self.return_block.pack(fill="x", pady=(12, 0))
            ret_date = self._format_flight_date(offer.return_date)
            self.leg_ret_lbl.configure(
                text=f"Return · {ret_date}" if ret_date else "Return"
            )
            ret_name = offer.return_airline or offer.airline or "—"
            self.return_airline_lbl.configure(text=ret_name)
            self._set_return_airline_logo(offer)
            self.ret_dep_time.configure(text=offer.return_depart_time)
            self.ret_arr_time.configure(text=offer.return_arrive_time or "--:--")
            self.ret_dep_airport.configure(text=offer.return_depart_airport or "—")
            self.ret_arr_airport.configure(text=offer.return_arrive_airport or "—")
            self.ret_duration.configure(text=offer.return_duration or "—")
            self.ret_stops.configure(text=offer.return_stops or "Direct")
            self.after(30, self._draw_ret_path)
        else:
            self.return_block.pack_forget()


class HotelRowCard(tk.Frame):
    """Trip.com-style hotel listing card (photo, name, stars, score, room, price)."""

    def __init__(self, master: tk.Misc, *, heading: str = "Recommended hotel", **kwargs) -> None:
        super().__init__(master, bg=C["paper"], **kwargs)
        self.heading_lbl = tk.Label(
            self,
            text=heading,
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        )
        self.heading_lbl.pack(fill="x", pady=(0, 6))

        shell = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="x")
        self.card = tk.Frame(shell, bg="#FFFFFF")
        self.card.pack(fill="x")

        body = tk.Frame(self.card, bg="#FFFFFF")
        body.pack(fill="x")

        # Full-bleed hotel photo across the bookings column
        self.photo = tk.Canvas(
            body, width=280, height=140, bg="#2A3340", highlightthickness=0
        )
        self.photo.pack(fill="x")
        self._hotel_photo: tk.PhotoImage | None = None
        self._PHOTO_W = 280
        self._PHOTO_H = 140
        self._offer_image_url = ""
        self._offer_photo_title = "Hotel"
        self._photo_reload_after: str | None = None
        self.photo.bind("<Configure>", self._on_photo_configure)
        self._draw_photo_placeholder()

        info = tk.Frame(body, bg="#FFFFFF", padx=10, pady=10)
        info.pack(fill="x")
        info.columnconfigure(0, weight=1)

        head = tk.Frame(info, bg="#FFFFFF")
        head.pack(fill="x")
        left_h = tk.Frame(head, bg="#FFFFFF")
        left_h.pack(side="left", fill="x", expand=True)
        self.name_lbl = tk.Label(
            left_h,
            text="Hotel",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=FONTS["hotel_name"],
            anchor="w",
        )
        self.name_lbl.pack(side="left")
        self.stars_lbl = tk.Label(
            left_h, text="", bg="#FFFFFF", fg="#E6A800", font=FONT_UI, anchor="w"
        )
        self.stars_lbl.pack(side="left", padx=(8, 0))

        score_box = tk.Frame(head, bg="#FFFFFF")
        score_box.pack(side="right")
        score_txt = tk.Frame(score_box, bg="#FFFFFF")
        score_txt.pack(side="left", padx=(0, 6))
        self.score_word = tk.Label(
            score_txt, text="Great", bg="#FFFFFF", fg=C["trip_blue"], font=FONT_UI_BOLD
        )
        self.score_word.pack(anchor="e")
        self.reviews_lbl = tk.Label(
            score_txt, text="", bg="#FFFFFF", fg=C["muted"], font=FONTS["tiny"]
        )
        self.reviews_lbl.pack(anchor="e")
        self.score_badge = tk.Label(
            score_box,
            text="—",
            bg="#1B3A6B",
            fg="#FFFFFF",
            font=FONTS["ui_bold"],
            padx=8,
            pady=4,
        )
        self.score_badge.pack(side="right")

        self.location_lbl = tk.Label(
            info,
            text="Loc · —",
            bg="#FFFFFF",
            fg=C["ink_soft"],
            font=FONT_SMALL,
            anchor="w",
        )
        self.location_lbl.pack(fill="x", pady=(8, 2))
        self.features_lbl = tk.Label(
            info,
            text="Highlights · —",
            bg="#FFFFFF",
            fg=C["ink_soft"],
            font=FONT_SMALL,
            anchor="w",
        )
        self.features_lbl.pack(fill="x", pady=(0, 8))

        mid = tk.Frame(info, bg="#FFFFFF")
        mid.pack(fill="x")
        mid.columnconfigure(0, weight=1)

        room = tk.Frame(mid, bg="#FFFFFF")
        room.grid(row=0, column=0, sticky="nw")
        accent = tk.Frame(room, bg="#D8DEE6", width=3)
        accent.pack(side="left", fill="y", padx=(0, 8))
        room_txt = tk.Frame(room, bg="#FFFFFF")
        room_txt.pack(side="left", fill="x")
        self.room_lbl = tk.Label(
            room_txt,
            text="Standard room",
            bg="#FFFFFF",
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        )
        self.room_lbl.pack(anchor="w")
        self.beds_lbl = tk.Label(
            room_txt, text="", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL, anchor="w"
        )
        self.beds_lbl.pack(anchor="w")
        self.social_lbl = tk.Label(
            room_txt, text="", bg="#FFFFFF", fg="#4A5560", font=FONTS["tiny"], anchor="w"
        )
        self.social_lbl.pack(anchor="w", pady=(4, 0))

        price_col = tk.Frame(mid, bg="#FFFFFF")
        price_col.grid(row=0, column=1, sticky="ne", padx=(12, 0))
        self.price_lbl = tk.Label(
            price_col,
            text="—",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=FONTS["price"],
        )
        self.price_lbl.pack(anchor="e")
        self.total_lbl = tk.Label(
            price_col,
            text="",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONTS["tiny"],
            wraplength=120,
            justify="right",
        )
        self.total_lbl.pack(anchor="e")
        self.fee_lbl = tk.Label(
            price_col,
            text="Additional charges may apply",
            bg="#FFFFFF",
            fg="#A0AAB4",
            font=("Segoe UI", 7),
        )
        self.fee_lbl.pack(anchor="e", pady=(2, 6))
        self.cta = tk.Label(
            price_col,
            text="Check Availability >",
            bg=C["trip_blue"],
            fg="#FFFFFF",
            font=FONT_UI_BOLD,
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.cta.pack(anchor="e")
        self._url = ""
        self.cta.bind("<Button-1>", self._open)
        self.cta.bind("<Enter>", lambda _e: self.cta.configure(bg=C["trip_blue_hover"]))
        self.cta.bind("<Leave>", lambda _e: self.cta.configure(bg=C["trip_blue"]))

        self.set_loading("Waiting for plan…")

    def _on_photo_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.width > 40 and abs(event.width - self._PHOTO_W) > 2:
            self._PHOTO_W = event.width
            self._PHOTO_H = max(120, event.height or self._PHOTO_H)
            # Canvas often grows after first paint — redraw so we don't leave a black half
            if self._offer_image_url:
                if self._photo_reload_after:
                    try:
                        self.after_cancel(self._photo_reload_after)
                    except Exception:
                        pass
                self._photo_reload_after = self.after(80, self._reload_hotel_photo)

    def _reload_hotel_photo(self) -> None:
        self._photo_reload_after = None
        if self._offer_image_url:
            self._paint_hotel_photo(self._offer_image_url, self._offer_photo_title)

    def _draw_photo_placeholder(self, title: str = "Hotel") -> None:
        self._hotel_photo = None
        self.photo.delete("all")
        w = max(self.photo.winfo_width(), self._PHOTO_W, 280)
        h = self._PHOTO_H
        self._PHOTO_W = w
        for i in range(10):
            t = i / 9
            color = _lerp_hex("#1A2230", "#C47A3A", t * 0.55)
            self.photo.create_rectangle(
                0, int(h * i / 10), w, int(h * (i + 1) / 10) + 1, outline="", fill=color
            )
        self.photo.create_rectangle(24, 36, 110, h - 14, fill="#243041", outline="")
        self.photo.create_rectangle(34, 46, 46, 58, fill="#F0C878", outline="")
        self.photo.create_rectangle(56, 46, 68, 58, fill="#F0C878", outline="")
        self.photo.create_rectangle(78, 46, 90, 58, fill="#E8B86A", outline="")
        self.photo.create_rectangle(34, 66, 46, 78, fill="#E8B86A", outline="")
        self.photo.create_rectangle(56, 66, 68, 78, fill="#F0C878", outline="")
        self.photo.create_rectangle(58, 86, 78, h - 14, fill="#1A2230", outline="")
        self.photo.create_oval(w - 32, 8, w - 10, 30, fill="#FFFFFF", outline="")
        self.photo.create_text(w - 21, 19, text="♡", fill="#1B3A6B", font=("Segoe UI", 10))
        self.photo.create_text(
            min(130, w // 2),
            18,
            text=(title[:18] if title else "Hotel"),
            fill="#FFFFFF",
            font=FONTS["tiny"],
        )

    def _paint_hotel_photo(self, url: str, title: str) -> None:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://hk.trip.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            data = urllib.request.urlopen(req, timeout=15).read()
            img = Image.open(BytesIO(data)).convert("RGB")
            self.update_idletasks()
            target_w = max(self.photo.winfo_width(), self._PHOTO_W, 280)
            target_h = max(self.photo.winfo_height(), self._PHOTO_H, 140)
            scale = max(target_w / max(img.width, 1), target_h / max(img.height, 1))
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - target_w) // 2)
            top = max(0, (new_h - target_h) // 2)
            fitted = resized.crop((left, top, left + target_w, top + target_h))
            photo = ImageTk.PhotoImage(fitted)
            self._hotel_photo = photo
            self._PHOTO_W = target_w
            self._PHOTO_H = target_h
            self.photo.delete("all")
            self.photo.create_image(0, 0, image=photo, anchor="nw")
        except Exception:
            self._draw_photo_placeholder(title or "Hotel")

    def _set_hotel_photo(self, offer: HotelOffer) -> None:
        url = (offer.image_url or "").strip().rstrip(".,;)")
        title = offer.name or "Hotel"
        if not url.startswith("http") or title.lower().startswith("hotels in "):
            try:
                from travel_agent.attraction_images import lookup_image

                city = offer.city or offer.location or "hotel"
                query = title if not title.lower().startswith("hotels in ") else f"{city} hotel exterior"
                found = lookup_image(query, city=city)
                if found and found.startswith("http"):
                    url = found
            except Exception:
                pass
        if not url.startswith("http"):
            self._offer_image_url = ""
            self._draw_photo_placeholder(title)
            return
        self._offer_image_url = url
        self._offer_photo_title = title
        self._paint_hotel_photo(url, title)

    def _open(self, _e: object | None = None) -> None:
        url = (self._url or "").strip()
        if url and is_openable_hotel_url(url):
            webbrowser.open(url)
            return
        if url and "trip.com" in url.lower() and "/hotels/" in url.lower():
            # List URL without city id still often works; open it
            webbrowser.open(url)
            return
        if url:
            messagebox.showwarning(
                "Invalid link",
                "No valid Trip.com hotel link for this stay yet. Regenerate the trip.",
            )
        else:
            messagebox.showinfo("No link", "Generate an itinerary first to get a booking link.")

    def set_loading(self, message: str) -> None:
        self._draw_photo_placeholder("…")
        self.name_lbl.configure(text=message)
        self.stars_lbl.configure(text="")
        self.score_badge.configure(text="—")
        self.score_word.configure(text="")
        self.reviews_lbl.configure(text="")
        self.location_lbl.configure(text="Searching Trip.com hotels…")
        self.features_lbl.configure(text="")
        self.room_lbl.configure(text="")
        self.beds_lbl.configure(text="")
        self.social_lbl.configure(text="")
        self.price_lbl.configure(text="…")
        self.total_lbl.configure(text="")
        self._url = ""

    def set_offer(self, offer: HotelOffer) -> None:
        heading = offer.stay_label or (
            f"Stay · {offer.city}" if offer.city else "Recommended hotel"
        )
        if offer.nights and "night" not in heading.lower():
            heading = f"{heading} ({offer.nights} night{'s' if offer.nights != 1 else ''})"
        self.heading_lbl.configure(text=heading)
        self._set_hotel_photo(offer)
        self.name_lbl.configure(text=offer.name)
        self.stars_lbl.configure(text="★" * max(0, min(offer.stars, 5)))
        self.score_badge.configure(text=offer.score or "—")
        self.score_word.configure(text=offer.score_label or "")
        self.reviews_lbl.configure(text=offer.reviews or "")
        loc = offer.location or offer.city or "—"
        dates = ""
        if offer.checkin and offer.checkout:
            dates = f"  ·  {offer.checkin} → {offer.checkout}"
        self.location_lbl.configure(text=f"Loc · {loc}{dates}")
        self.features_lbl.configure(text=f"Highlights · {offer.features}")
        self.room_lbl.configure(text=offer.room_type)
        self.beds_lbl.configure(text=offer.beds)
        self.social_lbl.configure(text=offer.social_proof)
        self.price_lbl.configure(text=offer.price_label)
        self.total_lbl.configure(text=offer.total_label or "Total (incl. taxes & fees): see Trip.com")
        # Prefer a city/date list URL when the offer link is missing or unusable
        url = (offer.url or "").strip()
        if not is_openable_hotel_url(url) and offer.city and offer.checkin and offer.checkout:
            url = build_hotel_list_url(
                city=offer.city,
                checkin=offer.checkin,
                checkout=offer.checkout,
            )
        elif is_trusted_hotel_detail_url(url):
            url = canonicalize_hotel_detail_url(
                url,
                checkin=offer.checkin or "",
                checkout=offer.checkout or "",
                city=offer.city or offer.location or "",
            ) or url
        self._url = url


class TravelAgentApp(tk.Tk):
    def __init__(self, model: str, headless: bool = True) -> None:
        super().__init__()
        self.model = model
        # Default: scrape Trip.com without a visible Chromium window
        self.headless = headless
        self.agent: TravelAgent | None = None
        self._busy = False
        self._wizard_step = 1
        self._last_plan = ""
        self._style_vars: dict[str, tk.BooleanVar] = {}
        self._trip_context: dict[str, str] = {}
        # Playwright sync API is thread-bound: one long-lived worker owns the browser.
        self._browser_jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._browser_thread: threading.Thread | None = None
        self._timetable_thumb_cache: dict[str, tk.PhotoImage] = {}
        self._font_step = DEFAULT_FONT_STEP
        apply_font_globals(self._font_step)

        self.title("Voyage — Trip.Planner-style Travel Agent")
        self.geometry("1080x780")
        self.minsize(880, 600)
        self.configure(bg=C["paper"])
        try:
            self.tk.call("tk", "scaling", 1.18)
        except tk.TclError:
            pass

        self.header = GradientHeader(self, bg=C["sky_top"])
        self.header.pack(fill="x")

        top_bar = tk.Frame(self, bg=C["paper"])
        top_bar.pack(fill="x", padx=28, pady=(12, 0))
        PillButton(
            top_bar,
            "Open Trip.Planner",
            command=lambda: webbrowser.open(TRIP_PLANNER_URL),
            primary=False,
            width=160,
        ).pack(side="right")
        self.font_slider = FontSizeSlider(
            top_bar,
            steps=FONT_SCALE_STEPS,
            value=self._font_step,
            command=self._on_font_scale,
            width=230,
            height=34,
        )
        self.font_slider.pack(side="right", padx=(0, 16))
        self.step_label = tk.Label(
            top_bar,
            text="Plan your trip",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
        )
        self.step_label.pack(side="left")

        # Page-level scroll container
        scroll_wrap = tk.Frame(self, bg=C["paper"])
        scroll_wrap.pack(fill="both", expand=True, padx=28, pady=(10, 18))

        self._page_canvas = tk.Canvas(
            scroll_wrap, bg=C["paper"], highlightthickness=0, bd=0
        )
        self._page_scroll = tk.Scrollbar(
            scroll_wrap, orient="vertical", command=self._page_canvas.yview
        )
        self._page_canvas.configure(yscrollcommand=self._page_scroll.set)
        self._page_scroll.pack(side="right", fill="y")
        self._page_canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self._page_canvas, bg=C["paper"])
        self._page_window = self._page_canvas.create_window(
            (0, 0), window=self.body, anchor="nw"
        )
        self.body.bind("<Configure>", self._on_page_body_configure)
        self._page_canvas.bind("<Configure>", self._on_page_canvas_configure)
        self._bind_mousewheel(self._page_canvas)
        self._bind_mousewheel(self.body)

        self.wizard = tk.Frame(self.body, bg=C["paper"])
        self.results = tk.Frame(self.body, bg=C["paper"])
        self._build_wizard()
        self._build_results()
        self._show_wizard_step(1)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._boot_agent)
        self.after(200, self._enable_page_scroll)

    def _on_font_scale(self, step: int) -> None:
        old = self._font_step
        if step == old:
            return
        old_f = _font_factor(old)
        new_f = _font_factor(step)
        self._font_step = step
        apply_font_globals(step)
        ratio = new_f / max(old_f, 0.01)
        self._rescale_widget_fonts(self, ratio)
        try:
            self.header._paint()
        except Exception:
            pass
        # Re-render itinerary cards so day/timetable fonts pick up new globals
        if self._last_plan and self.results.winfo_ismapped():
            try:
                parsed = self._apply_booking_urls(parse_itinerary(self._last_plan))
                parsed = self._ensure_days(parsed)
                self._render_parsed(parsed)
            except Exception:
                pass
        self.after(30, self._on_page_body_configure)

    def _rescale_widget_fonts(self, widget: tk.Misc, ratio: float) -> None:
        """Multiply existing widget font sizes by ratio (skip canvases)."""
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            if isinstance(child, FontSizeSlider):
                child._paint()
                continue
            if isinstance(child, (tk.Canvas,)):
                # Pill buttons / logos redraw via their own paint when possible
                paint = getattr(child, "_paint", None)
                if callable(paint):
                    try:
                        paint()
                    except Exception:
                        pass
                self._rescale_widget_fonts(child, ratio)
                continue
            try:
                current = str(child.cget("font"))
            except tk.TclError:
                current = ""
            if current:
                try:
                    actual = child.tk.call("font", "actual", current)
                    fam = "Segoe UI"
                    size = 11
                    weight = "normal"
                    slant = "roman"
                    if isinstance(actual, (list, tuple)):
                        opts = {
                            str(actual[i]): actual[i + 1]
                            for i in range(0, len(actual) - 1, 2)
                        }
                        fam = str(opts.get("-family", fam))
                        size = int(float(opts.get("-size", size)))
                        weight = str(opts.get("-weight", weight))
                        slant = str(opts.get("-slant", slant))
                    new_size = max(8, int(round(abs(size) * ratio)))
                    parts: list[Any] = [fam, new_size]
                    if weight == "bold":
                        parts.append("bold")
                    if slant == "italic":
                        parts.append("italic")
                    child.configure(font=tuple(parts))
                except Exception:
                    pass
            self._rescale_widget_fonts(child, ratio)

    def _on_page_body_configure(self, _event: object | None = None) -> None:
        self._page_canvas.configure(scrollregion=self._page_canvas.bbox("all"))

    def _on_page_canvas_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._page_canvas.itemconfigure(self._page_window, width=event.width)

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        def _on_wheel(event: tk.Event) -> str | None:  # type: ignore[type-arg]
            delta = int(-1 * (event.delta / 120)) if getattr(event, "delta", 0) else 0
            if delta:
                self._page_canvas.yview_scroll(delta, "units")
                return "break"
            return None

        def _on_linux_up(_event: tk.Event) -> str:  # type: ignore[type-arg]
            self._page_canvas.yview_scroll(-1, "units")
            return "break"

        def _on_linux_down(_event: tk.Event) -> str:  # type: ignore[type-arg]
            self._page_canvas.yview_scroll(1, "units")
            return "break"

        self._wheel_handlers = (_on_wheel, _on_linux_up, _on_linux_down)
        widget.bind("<MouseWheel>", _on_wheel, add="+")
        widget.bind("<Button-4>", _on_linux_up, add="+")
        widget.bind("<Button-5>", _on_linux_down, add="+")

    def _enable_page_scroll(self) -> None:
        """Route mouse wheel to the page canvas across nested widgets."""
        on_wheel, on_up, on_down = self._wheel_handlers
        self.bind_all("<MouseWheel>", on_wheel)
        self.bind_all("<Button-4>", on_up)
        self.bind_all("<Button-5>", on_down)

    def _scroll_to_top(self) -> None:
        self._page_canvas.yview_moveto(0)
        self.after(50, self._on_page_body_configure)

    # ── Wizard ──────────────────────────────────────────────────────────

    def _build_wizard(self) -> None:
        """Single-page trip form (destination + dates + style)."""
        self.wizard.pack(fill="both", expand=True)

        # Keep aliases so older step helpers still resolve to the same form
        self.step1 = tk.Frame(self.wizard, bg=C["paper"])
        self.step2 = self.step1
        self.step3 = self.step1
        form = self.step1

        tk.Label(
            form,
            text="Plan your trip",
            bg=C["paper"],
            fg=C["ink"],
            font=("Georgia", 26),
            anchor="w",
        ).pack(fill="x", pady=(24, 6))
        tk.Label(
            form,
            text="Destination, dates, and style on one page — then Voyage builds flights, hotels, and a day-by-day plan.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
            wraplength=720,
            justify="left",
        ).pack(fill="x", pady=(0, 18))

        self.dest_var = tk.StringVar()
        self.origin_var = tk.StringVar(value="Hong Kong")
        Field(form, "Destination (city or region)", self.dest_var).pack(fill="x", pady=(0, 12))
        Field(form, "Flying from", self.origin_var).pack(fill="x", pady=(0, 16))

        self.nights_var = tk.StringVar(value="7")
        self.depart_var = tk.StringVar(value=_default_depart())
        self.return_var = tk.StringVar(value=_default_return(7))
        self.budget_var = tk.StringVar(value="12000")
        self.depart_var.trace_add("write", self._sync_return)
        self.nights_var.trace_add("write", self._sync_return)

        grid = tk.Frame(form, bg=C["paper"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        Field(grid, "Nights", self.nights_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 12)
        )
        Field(grid, "Budget (HKD)", self.budget_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 12)
        )
        Field(grid, "Depart (YYYY-MM-DD)", self.depart_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 12)
        )
        Field(grid, "Return", self.return_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 16)
        )

        tk.Label(
            form,
            text="Travel style",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        ).pack(fill="x", pady=(4, 8))
        tk.Label(
            form,
            text="Pick one or more — pace and places follow your style.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
        ).pack(fill="x", pady=(0, 10))

        chips = tk.Frame(form, bg=C["paper"])
        chips.pack(fill="x", pady=(0, 20))
        for i, style in enumerate(TRAVEL_STYLES):
            var = tk.BooleanVar(value=(style == "First-time"))
            self._style_vars[style] = var
            Chip(chips, style, var).grid(row=i // 3, column=i % 3, padx=(0, 10), pady=6, sticky="w")

        row = tk.Frame(form, bg=C["paper"])
        row.pack(fill="x")
        self.generate_btn = PillButton(
            row, "Generate itinerary", command=self._on_generate, width=200
        )
        self.generate_btn.pack(side="left")

    def _sync_return(self, *_args: object) -> None:
        try:
            depart = date.fromisoformat(self.depart_var.get().strip())
            nights = max(1, int(self.nights_var.get().strip() or "7"))
        except ValueError:
            return
        self.return_var.set((depart + timedelta(days=nights)).isoformat())

    def _wizard_next(self, step: int) -> None:
        # Legacy multi-step navigation — form is now one page
        self._show_wizard_step(1)

    def _show_wizard_step(self, step: int = 1) -> None:
        self._wizard_step = 1
        self.results.pack_forget()
        self.wizard.pack(fill="both", expand=True)
        self.step1.pack(fill="both", expand=True)
        self.step_label.configure(text="Plan your trip")
        self._scroll_to_top()

    # ── Results board ───────────────────────────────────────────────────

    def _build_results(self) -> None:
        # Top actions
        actions = tk.Frame(self.results, bg=C["paper"])
        actions.pack(fill="x", pady=(0, 10))
        PillButton(
            actions, "New trip", command=lambda: self._show_wizard_step(1), primary=False, width=120
        ).pack(side="left")
        PillButton(
            actions, "Show raw plan", command=self._show_raw, primary=False, width=140
        ).pack(side="left", padx=8)
        self.regen_btn = PillButton(
            actions, "Regenerate", command=self._on_generate, primary=False, width=130
        )
        self.regen_btn.pack(side="left")

        split = tk.Frame(self.results, bg=C["paper"])
        split.pack(fill="x", anchor="n")
        # Narrow bookings column (~32%); itinerary gets the rest
        split.columnconfigure(0, weight=1, minsize=240)
        split.columnconfigure(1, weight=3, minsize=440)

        left = tk.Frame(split, bg=C["paper"])
        left.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        right = tk.Frame(split, bg=C["paper"])
        right.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            left, text="Bookings", bg=C["paper"], fg=C["ink"], font=FONT_DISPLAY, anchor="w"
        ).pack(fill="x", pady=(0, 8))
        self.flight_row = FlightRowCard(left)
        self.flight_row.pack(fill="x", pady=(0, 12))
        self.hotels_box = tk.Frame(left, bg=C["paper"])
        self.hotels_box.pack(fill="x", pady=(0, 8))
        self.hotel_row = HotelRowCard(self.hotels_box)
        self.hotel_row.pack(fill="x", pady=(0, 8))
        self.hotel_rows: list[HotelRowCard] = [self.hotel_row]

        tk.Label(
            right,
            text="Day-by-day itinerary",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_DISPLAY,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.days_duration = tk.Label(
            right,
            text="",
            bg="#FFFFFF",
            fg=C["ink"],
            font=FONT_SMALL,
            anchor="w",
            padx=10,
            pady=4,
        )
        self.days_duration.pack(fill="x", pady=(0, 8))

        self.days_inner = tk.Frame(right, bg="#2C333A")
        self.days_inner.pack(fill="x", anchor="n", ipadx=8, ipady=8)
        self.days_inner.bind("<Configure>", lambda _e: self._on_page_body_configure())

        # Refine chat dock
        chat_wrap = tk.Frame(self.results, bg=C["paper"])
        chat_wrap.pack(fill="x", pady=(12, 0))
        tk.Label(
            chat_wrap,
            text="Refine with AI",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            chat_wrap,
            text="Like Trip.Planner’s floating assistant — ask to tweak days, hotels, or flights.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        self.chat_out = scrolledtext.ScrolledText(
            chat_wrap,
            height=5,
            wrap="word",
            font=FONT_BODY,
            bg=C["field"],
            fg=C["ink"],
            relief="flat",
            bd=1,
            highlightthickness=1,
            highlightbackground=C["line"],
            padx=10,
            pady=8,
            state="disabled",
        )
        self.chat_out.pack(fill="x")
        self.chat_out.tag_configure("you", foreground=C["accent_deep"], font=FONT_UI_BOLD)
        self.chat_out.tag_configure("agent", foreground=C["ok"], font=FONT_UI_BOLD)
        self.chat_out.tag_configure("url", foreground=C["accent"], underline=True)
        self.chat_out.bind("<Button-1>", self._on_click_url)

        row = tk.Frame(chat_wrap, bg=C["paper"])
        row.pack(fill="x", pady=(8, 0))
        self.chat_var = tk.StringVar()
        shell = tk.Frame(row, bg=C["line"], padx=1, pady=1)
        shell.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry = tk.Entry(
            shell,
            textvariable=self.chat_var,
            font=FONT_INPUT,
            bg=C["field"],
            fg=C["ink"],
            insertbackground=C["ink"],
            relief="flat",
            bd=0,
        )
        entry.pack(fill="x", ipady=10, ipadx=10)
        entry.bind("<Return>", lambda _e: self._on_refine())
        PillButton(row, "Send", command=self._on_refine, width=100).pack(side="left")

    def _make_card(self, parent: tk.Misc, title: str) -> dict[str, tk.Misc]:
        shell = tk.Frame(parent, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="x", pady=(0, 10))
        card = tk.Frame(shell, bg=C["card"], padx=14, pady=12)
        card.pack(fill="x")
        tk.Label(card, text=title, bg=C["card"], fg=C["ink"], font=FONT_UI_BOLD, anchor="w").pack(
            fill="x"
        )
        body = tk.Label(
            card,
            text="Waiting for plan…",
            bg=C["card"],
            fg=C["muted"],
            font=FONT_BODY,
            justify="left",
            anchor="nw",
            wraplength=320,
        )
        body.pack(fill="x", pady=(6, 4))
        links = tk.Frame(card, bg=C["card"])
        links.pack(fill="x")
        return {"shell": shell, "card": card, "body": body, "links": links}

    def _show_results(self) -> None:
        self.wizard.pack_forget()
        self.results.pack(fill="both", expand=True)
        self.step_label.configure(text="Your itinerary")
        self._scroll_to_top()

    def _render_hotel_cards(self, parsed: ParsedItinerary) -> None:
        """Show one or more hotel stay cards (multi-city regions use several)."""
        from travel_agent.itinerary_parse import HotelOffer, parse_hotel_offer

        offers: list[HotelOffer] = list(getattr(parsed, "hotel_offers", None) or [])
        if not offers and parsed.hotel_offer:
            offers = [parsed.hotel_offer]
        if not offers and parsed.hotel:
            offers = [
                parse_hotel_offer(
                    parsed.hotel.body,
                    fallback_url=parsed.hotel.urls[0] if parsed.hotel.urls else "",
                )
            ]
        if not offers:
            stub = parse_hotel_offer(parsed.raw or "")
            dest = to_hotel_city(self._trip_context.get("destination", "") or "")
            if dest and stub.name in {"", "Recommended hotel"}:
                stub.name = f"Hotels in {dest}"
                stub.location = dest
            offers = [stub]

        # Rebuild hotel cards to match stay count
        for child in self.hotels_box.winfo_children():
            child.destroy()
        self.hotel_rows = []
        for i, offer in enumerate(offers[:4]):
            card = HotelRowCard(self.hotels_box)
            card.pack(fill="x", pady=(0, 10))
            card.set_offer(offer)
            self.hotel_rows.append(card)
            if i == 0:
                self.hotel_row = card

    def _clear_days(self) -> None:
        for child in self.days_inner.winfo_children():
            child.destroy()

    def _fill_card(self, card: dict[str, tk.Misc], text: str, urls: list[str]) -> None:
        body: tk.Label = card["body"]  # type: ignore[assignment]
        links: tk.Frame = card["links"]  # type: ignore[assignment]
        for child in links.winfo_children():
            child.destroy()
        preview = text.strip() if text.strip() else "No details returned."
        if len(preview) > 480:
            preview = preview[:480] + "…"
        body.configure(text=preview, fg=C["ink"])
        for url in urls[:3]:
            LinkLabel(links, url, wraplength=300).pack(anchor="w", pady=2)

    def _render_parsed(self, parsed: ParsedItinerary) -> None:
        from travel_agent.itinerary_parse import parse_flight_offer, parse_hotel_offer

        if parsed.flight_offer:
            self.flight_row.set_offer(parsed.flight_offer)
        elif parsed.flight:
            offer = parse_flight_offer(
                parsed.flight.body,
                fallback_url=parsed.flight.urls[0] if parsed.flight.urls else "",
            )
            self.flight_row.set_offer(offer)
        else:
            # Still show a context-based stub rather than an empty loading row
            stub = parse_flight_offer(parsed.raw or "")
            ctx = self._trip_context
            stub.depart_airport = to_flight_code(ctx.get("origin", "Hong Kong") or "Hong Kong").upper() or "HKG"
            arrive = (ctx.get("arrive_airport") or "").strip().upper()
            if not arrive:
                arrive = to_flight_code(ctx.get("destination", "") or "").upper() or "—"
            stub.arrive_airport = arrive
            ret_from = (ctx.get("depart_airport") or "").strip().upper()
            if ret_from:
                stub.return_depart_airport = ret_from
                stub.return_arrive_airport = stub.depart_airport
            self.flight_row.set_offer(stub)

        self._render_hotel_cards(parsed)

        self._clear_days()
        nights = 0
        try:
            depart = self._trip_context.get("depart_date") or ""
            ret = self._trip_context.get("return_date") or ""
            if depart and ret:
                nights = max(1, (date.fromisoformat(ret) - date.fromisoformat(depart)).days)
        except ValueError:
            nights = len(parsed.days) or 0
        if not nights:
            nights = len(parsed.days)
        if hasattr(self, "days_duration"):
            if nights:
                self.days_duration.configure(
                    text=f"  Trip Duration: {nights} Day{'s' if nights != 1 else ''}"
                )
            else:
                self.days_duration.configure(text="")

        if not parsed.days:
            # Should be unreachable after _ensure_days; keep a quiet placeholder
            empty = tk.Frame(self.days_inner, bg="#FFFFFF", padx=14, pady=14)
            empty.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
            tk.Label(
                empty,
                text="Day plan will appear here once the itinerary is ready.",
                bg="#FFFFFF",
                fg=C["muted"],
                font=FONT_BODY,
                justify="left",
                anchor="nw",
            ).pack(fill="x")
            self.after(80, self._on_page_body_configure)
            return

        self.days_inner.columnconfigure(0, weight=1)
        self._timetable_thumb_seq = 0
        for idx, day in enumerate(parsed.days):
            self._add_day_card(day, index=idx)

        self.after(80, self._on_page_body_configure)

    def _day_calendar_date(self, index: int) -> str:
        """Calendar date for day card index 0 = trip depart date."""
        depart = (self._trip_context.get("depart_date") or "").strip()
        if not depart:
            return ""
        try:
            d = date.fromisoformat(depart) + timedelta(days=max(0, index))
        except ValueError:
            return ""
        return d.strftime("%a, %d %b %Y")

    def _day_theme_icon(self, text: str) -> str:
        """Pick a simple footer icon from the day's activity text."""
        low = (text or "").lower()
        if any(k in low for k in ("flight", "airport", "arrive", "depart", "transfer")):
            return "✈"
        if any(k in low for k in ("dinner", "lunch", "food", "cuisine", "ramen", "sushi", "cafe")):
            return "🍽"
        if any(k in low for k in ("temple", "shrine", "palace", "museum", "asakusa", "senso")):
            return "⛩"
        if any(k in low for k in ("shop", "market", "ginza", "harajuku")):
            return "✦"
        if any(k in low for k in ("park", "garden", "hike", "nature", "ueno")):
            return "❀"
        if any(k in low for k in ("hotel", "check-in", "check in", "checkout", "check-out")):
            return "⌂"
        return "◎"

    def _placeholder_timetable_thumb(
        self,
        label: str,
        *,
        size: tuple[int, int] = (160, 110),
        accent: str = "#C62828",
    ) -> tk.PhotoImage:
        """Solid accent tile used when a photo URL is missing or fails to load."""
        tw, th = size
        cache_key = f"ph|{accent}|{(label or '')[:24]}|{tw}x{th}"
        if cache_key in self._timetable_thumb_cache:
            return self._timetable_thumb_cache[cache_key]
        img = Image.new("RGB", (tw, th), accent)
        # Soft overlay so tiles aren't flat blocks of one color
        overlay = Image.new("RGB", (tw, th), "#FFFFFF")
        img = Image.blend(img, overlay, 0.72)
        photo = ImageTk.PhotoImage(img)
        self._timetable_thumb_cache[cache_key] = photo
        return photo

    def _photo_from_bytes(self, data: bytes, *, size: tuple[int, int] = (160, 110)) -> tk.PhotoImage | None:
        """Build a cropped PhotoImage from raw image bytes (main thread only)."""
        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            tw, th = size
            scale = max(tw / max(img.width, 1), th / max(img.height, 1))
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - tw) // 2)
            top = max(0, (new_h - th) // 2)
            fitted = resized.crop((left, top, left + tw, top + th))
            return ImageTk.PhotoImage(fitted)
        except Exception:
            return None

    def _load_timetable_thumb(self, url: str, *, size: tuple[int, int] = (160, 110)) -> tk.PhotoImage | None:
        """Download and cache a larger attraction thumbnail for a timetable row."""
        from travel_agent.attraction_images import fetch_image_bytes, sanitize_image_url

        url = sanitize_image_url(url)
        if not url.startswith("http"):
            return None
        cache_key = f"{url}|{size[0]}x{size[1]}"
        if cache_key in self._timetable_thumb_cache:
            return self._timetable_thumb_cache[cache_key]
        try:
            data = fetch_image_bytes(url)
            if not data:
                return None
            photo = self._photo_from_bytes(data, size=size)
            if photo is None:
                return None
            self._timetable_thumb_cache[cache_key] = photo
            return photo
        except Exception:
            return None

    def _bind_timetable_thumb_async(
        self,
        img_lbl: tk.Label,
        url: str,
        *,
        detail: str = "",
        accent: str = "#C62828",
        size: tuple[int, int] = (160, 110),
        delay_ms: int = 0,
        used_ids: set[str] | None = None,
    ) -> None:
        """Fetch bytes off-thread; build PhotoImage on the UI thread."""
        from travel_agent.attraction_images import (
            _image_identity,
            candidate_image_urls,
            fetch_image_bytes,
            sanitize_image_url,
        )

        dest = self._trip_context.get("destination", "") or ""
        avoid = set(used_ids or ())
        urls: list[str] = []
        first = sanitize_image_url(url) if url else ""
        if first:
            urls.append(first)
            avoid.discard(_image_identity(first))  # own assigned url is allowed
        for cand in candidate_image_urls(detail, dest, limit=10, exclude=avoid):
            if cand not in urls:
                urls.append(cand)

        for u in list(urls):
            cache_key = f"{u}|{size[0]}x{size[1]}"
            cached = self._timetable_thumb_cache.get(cache_key)
            if cached is not None:
                img_lbl.configure(image=cached)
                img_lbl.image = cached
                if used_ids is not None:
                    used_ids.add(_image_identity(u))
                return

        def _apply(data: bytes | None, key: str, chosen: str) -> None:
            if not img_lbl.winfo_exists() or not data:
                return
            photo = self._timetable_thumb_cache.get(key)
            if photo is None:
                photo = self._photo_from_bytes(data, size=size)
                if photo is None:
                    return
                self._timetable_thumb_cache[key] = photo
            img_lbl.configure(image=photo)
            img_lbl.image = photo
            if used_ids is not None and chosen:
                used_ids.add(_image_identity(chosen))

        def _worker() -> None:
            data = b""
            key = ""
            chosen = ""
            claimed = set(used_ids or ())
            if first:
                claimed.discard(_image_identity(first))
            for cand in urls:
                ident = _image_identity(cand)
                if ident and ident in claimed and cand != first:
                    continue
                data = fetch_image_bytes(cand)
                if data:
                    key = f"{cand}|{size[0]}x{size[1]}"
                    chosen = cand
                    break
            self.after(0, lambda d=data, k=key, c=chosen: _apply(d, k, c))

        self.after(max(0, delay_ms), lambda: threading.Thread(target=_worker, daemon=True).start())

    def _draw_down_arrow_icon(self, parent: tk.Misc, *, color: str, size: int = 18) -> tk.Canvas:
        """Small canvas arrow icon (shaft + chevron head) for transfer rows."""
        canvas = tk.Canvas(
            parent,
            width=size,
            height=size + 4,
            bg="#FFFFFF",
            highlightthickness=0,
            bd=0,
        )
        cx = size // 2
        top = 2
        tip = size + 1
        # vertical shaft
        canvas.create_line(cx, top, cx, tip - 5, fill=color, width=2, capstyle=tk.ROUND)
        # arrow head
        canvas.create_polygon(
            cx,
            tip,
            cx - 5,
            tip - 7,
            cx + 5,
            tip - 7,
            fill=color,
            outline=color,
        )
        return canvas

    def _add_day_card(self, day: object, *, index: int = 0) -> None:
        """Seoul-itinerary style: big day number, DAY badge, list, dark footer."""
        title = getattr(day, "title", "Day") or "Day"
        body = getattr(day, "body", "") or ""
        urls = list(getattr(day, "urls", []) or [])
        from travel_agent.attraction_images import (
            _image_identity,
            images_for_timetable,
            lookup_image,
        )

        dest = self._trip_context.get("destination", "") or ""
        slot_images: dict[str, str] = dict(getattr(day, "images", {}) or {})
        if body:
            # Always fill every timed row with distinct photos
            filled = images_for_timetable(body, dest)
            slot_images = filled or slot_images
        used_ids: set[str] = {
            _image_identity(u) for u in slot_images.values() if _image_identity(u)
        }

        m = re.match(r"(?i)^day\s+(\d+)\b", title)
        if m:
            num = int(m.group(1))
        elif re.match(r"(?i)^final\b", title):
            num = index + 1
        else:
            num = index + 1
        num_label = f"{num:02d}"
        accent = _DAY_ACCENTS[index % len(_DAY_ACCENTS)]

        if "—" in title:
            _heading, subtitle = [p.strip() for p in title.split("—", 1)]
        elif " - " in title:
            _heading, subtitle = [p.strip() for p in title.split(" - ", 1)]
        else:
            subtitle = ""

        day_date = self._day_calendar_date(index)

        raw_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        activities: list[str] = []
        for ln in raw_lines:
            ln = re.sub(r"^[•\-\*]\s*", "", ln)
            ln = re.sub(r"^\d+[\.)]\s*", "", ln)
            if ln:
                activities.append(ln)
        if not activities:
            activities = ["Details coming soon."]

        wrap = tk.Frame(self.days_inner, bg="#2C333A")
        wrap.grid(row=index, column=0, sticky="ew", padx=8, pady=8)

        card = tk.Frame(wrap, bg="#FFFFFF")
        card.pack(fill="both", expand=True)

        head = tk.Frame(card, bg="#FFFFFF")
        head.pack(fill="x", padx=14, pady=(12, 4))
        left_head = tk.Frame(head, bg="#FFFFFF")
        left_head.pack(side="left", fill="x", expand=True)
        tk.Label(
            left_head,
            text=num_label,
            bg="#FFFFFF",
            fg=accent,
            font=FONT_DAY_NUM,
            anchor="w",
        ).pack(side="left")
        if day_date:
            tk.Label(
                left_head,
                text=day_date,
                bg="#FFFFFF",
                fg=C["ink"],
                font=FONT_UI_BOLD,
                anchor="w",
            ).pack(side="left", padx=(12, 0), pady=(10, 0))
        tk.Label(
            head,
            text=" DAY ",
            bg=accent,
            fg="#FFFFFF",
            font=FONT_DAY_BADGE,
            padx=6,
            pady=3,
        ).pack(side="right", anchor="n", pady=(6, 0))

        if subtitle:
            tk.Label(
                card,
                text=subtitle,
                bg="#FFFFFF",
                fg=C["ink_soft"],
                font=FONT_SMALL,
                anchor="w",
            ).pack(fill="x", padx=14, pady=(0, 4))

        # Timetable header
        body_frame = tk.Frame(card, bg="#FFFFFF")
        body_frame.pack(fill="both", expand=True, padx=14, pady=(4, 10))
        tk.Label(
            body_frame,
            text="TIMETABLE",
            bg="#FFFFFF",
            fg=accent,
            font=FONT_DAY_BADGE,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        for line in activities[:16]:
            transfer = re.match(r"^↓\s*(.+?)\s*·\s*~?(\d+)\s*min\.?$", line.strip())
            if not transfer:
                # tolerate "↓ Method ~12 min" without middle dot
                transfer = re.match(r"^↓\s*(.+?)\s+~?(\d+)\s*min\.?$", line.strip())
            row = tk.Frame(body_frame, bg="#FFFFFF")
            row.pack(fill="x", pady=2)
            if transfer:
                method = transfer.group(1).strip()
                mins = transfer.group(2).strip()
                link = tk.Frame(row, bg="#FFFFFF")
                link.pack(fill="x", padx=(34, 0), pady=(2, 4))
                arrow = self._draw_down_arrow_icon(link, color=accent, size=16)
                arrow.pack(side="left", padx=(0, 8))
                tk.Label(
                    link,
                    text=f"{method}  ·  ~{mins} min",
                    bg="#FFFFFF",
                    fg=C["ink_soft"],
                    font=FONT_SMALL,
                    anchor="w",
                ).pack(side="left")
                continue

            tm = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)\s+(.*)$", line)
            if tm:
                time_txt = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                detail = tm.group(3).strip()
                tk.Label(
                    row,
                    text=time_txt,
                    bg="#F3F6F8",
                    fg=accent,
                    font=("Consolas", 10, "bold"),
                    padx=8,
                    pady=4,
                    width=6,
                    anchor="center",
                ).pack(side="left", padx=(0, 10))
                text_wrap = tk.Frame(row, bg="#FFFFFF")
                text_wrap.pack(side="left", fill="x", expand=True)
                tk.Label(
                    text_wrap,
                    text=detail,
                    bg="#FFFFFF",
                    fg="#1A1A1A",
                    font=FONT_BODY,
                    justify="left",
                    anchor="w",
                    wraplength=280,
                ).pack(side="left", fill="x", expand=True)
                img_url = slot_images.get(time_txt, "") or lookup_image(
                    detail, dest, exclude=used_ids
                )
                if img_url:
                    used_ids.add(_image_identity(img_url))
                # Show placeholder immediately; swap real photo in asynchronously
                # (avoids Wikimedia 429 when many thumbs load at once).
                placeholder = self._placeholder_timetable_thumb(
                    detail, size=(160, 110), accent=accent
                )
                img_lbl = tk.Label(
                    row,
                    image=placeholder,
                    bg="#FFFFFF",
                    bd=1,
                    relief="solid",
                    highlightthickness=0,
                )
                img_lbl.image = placeholder
                img_lbl.pack(side="right", padx=(10, 0))
                if img_url:
                    seq = getattr(self, "_timetable_thumb_seq", 0)
                    self._timetable_thumb_seq = seq + 1
                    self._bind_timetable_thumb_async(
                        img_lbl,
                        img_url,
                        detail=detail,
                        accent=accent,
                        size=(160, 110),
                        delay_ms=120 * seq,
                        used_ids=used_ids,
                    )
                row.configure(height=118)
                row.pack_propagate(False)
            else:
                tk.Label(
                    row,
                    text=line,
                    bg="#FFFFFF",
                    fg="#1A1A1A",
                    font=FONT_BODY,
                    justify="left",
                    anchor="w",
                    wraplength=480,
                ).pack(fill="x")

        for url in urls[:1]:
            LinkLabel(body_frame, url, bg="#FFFFFF", wraplength=480).pack(anchor="w", pady=(6, 0))

        footer = tk.Frame(card, bg="#3D4F5F", height=36)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        tk.Label(
            footer,
            text=self._day_theme_icon(body + " " + title),
            bg="#3D4F5F",
            fg="#FFFFFF",
            font=("Segoe UI", 14),
        ).pack(expand=True)

    def _show_raw(self) -> None:
        if not self._last_plan:
            return
        win = tk.Toplevel(self)
        win.title("Raw plan")
        win.geometry("720x520")
        txt = scrolledtext.ScrolledText(win, wrap="word", font=FONT_BODY, padx=12, pady=12)
        txt.pack(fill="both", expand=True)
        txt.insert("end", self._last_plan)
        txt.configure(state="disabled")

    # ── Agent actions ───────────────────────────────────────────────────

    def _selected_styles(self) -> list[str]:
        return [name for name, var in self._style_vars.items() if var.get()]

    def _on_generate(self) -> None:
        if self._busy:
            return
        if not self.agent:
            messagebox.showwarning("Not ready", "Browser is still starting.")
            return
        destination = self.dest_var.get().strip()
        if not destination:
            messagebox.showerror("Destination", "Enter a destination city.")
            self._show_wizard_step()
            return
        styles = self._selected_styles()
        if not styles:
            messagebox.showerror("Travel style", "Pick at least one travel style.")
            self._show_wizard_step()
            return
        try:
            budget = float(self.budget_var.get().strip())
            nights = max(1, int(self.nights_var.get().strip() or "7"))
            depart = self.depart_var.get().strip()
            ret = self.return_var.get().strip() or None
            date.fromisoformat(depart)
            if ret:
                date.fromisoformat(ret)
        except ValueError:
            messagebox.showerror("Duration", "Check nights, dates, and budget.")
            self._show_wizard_step()
            return

        query = build_plan_query(
            destination=destination,
            depart_date=depart,
            return_date=ret,
            budget_hkd=budget,
            origin=self.origin_var.get().strip() or "Hong Kong",
            travel_styles=styles,
            nights=nights,
            include_flights=True,
            include_trains=True,
            include_transfers=True,
            rent_car=False,
        )
        self._trip_context = {
            "origin": self.origin_var.get().strip() or "Hong Kong",
            "destination": destination,
            "depart_date": depart,
            "return_date": ret or "",
        }
        # Vague regions (e.g. California) → multi-city open-jaw itinerary
        try:
            from travel_agent.regions import build_regional_route

            route = build_regional_route(destination, nights, depart_date=depart)
            if route:
                self._trip_context["arrive_airport"] = route.arrive_airport.upper()
                self._trip_context["depart_airport"] = route.depart_airport.upper()
                self._trip_context["region"] = route.label
                self._trip_context["stays"] = ";".join(
                    f"{s.city}|{s.nights}|{s.checkin}|{s.checkout}|{s.airport}"
                    for s in route.stays
                )
                self._trip_context["internal_note"] = route.internal_note
        except Exception:
            pass
        if self.agent:
            self.agent.booking_links = {"flight": "", "hotel": "", "hotel_name": ""}

        self._show_results()
        self.flight_row.set_loading("Comparing flights on Trip.com…")
        self.hotel_row.set_loading("Comparing hotels on Trip.com…")
        self._clear_days()
        loading = tk.Frame(self.days_inner, bg="#FFFFFF", padx=14, pady=16)
        loading.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        tk.Label(
            loading,
            text="Building your itinerary… this can take a few minutes.",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONT_BODY,
            anchor="w",
        ).pack(anchor="w")
        self._set_busy(True, "Building Trip.Planner-style itinerary…")

        def job() -> str:
            assert self.agent is not None
            answer = self.agent.chat(query)
            # Live card refresh must not wipe a finished itinerary on navigation errors
            try:
                self._refresh_live_booking_cards()
            except Exception:
                pass
            return answer

        self._run_browser_job(
            job,
            on_ok=lambda answer: self._apply_plan(answer),
            on_err=lambda e: self._apply_plan(f"Error: {e}"),
            done=lambda: self._set_busy(False),
        )

    def _refresh_live_booking_cards(self) -> None:
        """Refresh flight/hotel card fields from Trip.com (browser thread only)."""
        if not self.agent:
            return
        ctx = self._trip_context
        checkout = ctx.get("return_date") or ""
        if not checkout and ctx.get("depart_date"):
            try:
                checkout = (
                    date.fromisoformat(ctx["depart_date"]) + timedelta(days=7)
                ).isoformat()
            except ValueError:
                checkout = ctx["depart_date"]

        # Prefer first regional stay city (not vague "California") for hotel refresh
        hotel_city = ""
        hotel_checkout = checkout
        stays_raw = (ctx.get("stays") or "").strip()
        if stays_raw:
            first = stays_raw.split(";")[0].split("|")
            if first:
                hotel_city = first[0]
            if len(first) > 3 and first[3]:
                hotel_checkout = first[3]
            if len(first) > 2 and first[2]:
                hotel_checkin = first[2]
            else:
                hotel_checkin = ctx.get("depart_date") or ""
        else:
            hotel_city = to_hotel_city(ctx.get("destination", "") or "") or ctx.get(
                "destination", ""
            )
            hotel_checkin = ctx.get("depart_date") or ""

        arrive = (ctx.get("arrive_airport") or "").strip().upper()
        ret_from = (ctx.get("depart_airport") or "").strip().upper()
        open_jaw = bool(arrive and ret_from and arrive != ret_from)

        # Hotel photo/details first — flight goto must not discard hotel image work
        try:
            # Skip when plan_trip already filled multi-city stays (refresh would
            # collapse them into one California/SFO hotel).
            multi_stays = list(
                getattr(self.agent.browser, "last_hotel_stays", None) or []
            )
            if len(multi_stays) > 1:
                pass
            elif not is_trusted_hotel_detail_url(self.agent.booking_links.get("hotel", "")):
                if hotel_city and hotel_checkin:
                    live = fetch_hotel_detail_link(
                        self.agent.browser,
                        city=hotel_city,
                        checkin=hotel_checkin,
                        checkout=hotel_checkout or checkout,
                    )
                    self._merge_live_hotel(live)
            self._ensure_hotel_image()
        except Exception:
            pass

        try:
            # Never re-scrape vague region as same-city RT — that overwrites open-jaw
            if open_jaw:
                links = self.agent.booking_links
                links["flight_to"] = arrive
                links["flight_return_from"] = ret_from
                links["flight_return_to"] = to_flight_code(
                    ctx.get("origin", "Hong Kong") or "Hong Kong"
                ).upper() or "HKG"
                if ctx.get("return_date"):
                    links["flight_return_date"] = ctx["return_date"]
            elif ctx.get("origin") and ctx.get("destination") and ctx.get("depart_date"):
                dest_code = arrive or to_flight_code(ctx["destination"]).upper()
                live_flight = fetch_flight_card(
                    self.agent.browser,
                    origin=ctx["origin"],
                    destination=dest_code or ctx["destination"],
                    depart_date=ctx["depart_date"],
                    return_date=ctx.get("return_date") or checkout,
                )
                self._merge_live_flight(live_flight)
        except Exception:
            pass

    def _merge_live_hotel(self, live: dict[str, str]) -> None:
        """Copy scraped hotel detail fields into agent.booking_links."""
        if not self.agent:
            return
        if live.get("url"):
            self.agent.booking_links["hotel"] = live["url"]
        if live.get("name"):
            self.agent.booking_links["hotel_name"] = live["name"]
        for key in (
            "hotel_price",
            "hotel_total",
            "hotel_stars",
            "hotel_score",
            "hotel_location",
            "hotel_reviews",
            "hotel_image",
        ):
            if live.get(key):
                self.agent.booking_links[key] = live[key]

    def _ensure_hotel_image(self) -> None:
        """Scrape hotel cover photo from the detail page when missing."""
        if not self.agent:
            return
        links = self.agent.booking_links
        if links.get("hotel_image"):
            return
        url = links.get("hotel", "")
        if not is_trusted_hotel_detail_url(url):
            return
        scrape = getattr(self.agent.browser, "scrape_hotel_image_url", None)
        if not scrape:
            return
        try:
            img = scrape(url)
        except Exception:
            img = ""
        if img:
            links["hotel_image"] = img

    def _merge_live_flight(self, live: dict[str, str]) -> None:
        """Copy scraped flight card fields into agent.booking_links."""
        if not self.agent:
            return
        if live.get("flight"):
            self.agent.booking_links["flight"] = live["flight"]
        for key in (
            "flight_airline",
            "flight_depart",
            "flight_arrive",
            "flight_from",
            "flight_to",
            "flight_duration",
            "flight_stops",
            "flight_price",
            "flight_airline_logo",
            "flight_date",
            "flight_return_airline",
            "flight_return_depart",
            "flight_return_arrive",
            "flight_return_from",
            "flight_return_to",
            "flight_return_duration",
            "flight_return_stops",
            "flight_return_airline_logo",
            "flight_return_date",
        ):
            if live.get(key):
                self.agent.booking_links[key] = live[key]

    def _built_booking_urls(self) -> tuple[str, str]:
        ctx = self._trip_context
        if not ctx.get("destination") or not ctx.get("depart_date"):
            return "", ""
        ret = ctx.get("return_date") or None
        arrive = (ctx.get("arrive_airport") or "").strip().upper()
        ret_from = (ctx.get("depart_airport") or "").strip().upper()
        flight_dest = arrive or ctx["destination"]
        # Open-jaw: link outbound leg (return is a separate OW search in plan_trip)
        trip_type = "oneway" if (arrive and ret_from and arrive != ret_from) else "roundtrip"
        flight = build_flight_search_url(
            origin=ctx.get("origin", "Hong Kong"),
            destination=flight_dest,
            depart_date=ctx["depart_date"],
            return_date=None if trip_type == "oneway" else ret,
            trip_type=trip_type,
        )
        checkout = ret
        hotel_city = ctx["destination"]
        hotel_checkin = ctx["depart_date"]
        stays_raw = (ctx.get("stays") or "").strip()
        if stays_raw:
            bits = stays_raw.split(";")[0].split("|")
            if bits:
                hotel_city = bits[0]
            if len(bits) > 2 and bits[2]:
                hotel_checkin = bits[2]
            if len(bits) > 3 and bits[3]:
                checkout = bits[3]
        if not checkout:
            try:
                checkout = (
                    date.fromisoformat(ctx["depart_date"]) + timedelta(days=7)
                ).isoformat()
            except ValueError:
                checkout = ctx["depart_date"]
        hotel = build_hotel_list_url(
            city=hotel_city,
            checkin=hotel_checkin,
            checkout=checkout,
        )
        return flight, hotel

    def _apply_booking_urls(self, parsed: ParsedItinerary) -> ParsedItinerary:
        """Override LLM-parsed links with live Trip.com tool URLs and enrich cards."""
        from travel_agent.itinerary_parse import parse_flight_offer, parse_hotel_offer

        tool_flight = ""
        tool_hotel = ""
        hotel_name = ""
        if self.agent:
            tool_flight = self.agent.booking_links.get("flight", "")
            tool_hotel = self.agent.booking_links.get("hotel", "")
            hotel_name = self.agent.booking_links.get("hotel_name", "")
        built_flight, built_hotel = self._built_booking_urls()

        parsed_flight = ""
        if parsed.flight_offer:
            parsed_flight = parsed.flight_offer.url
        elif parsed.flight and parsed.flight.urls:
            parsed_flight = parsed.flight.urls[0]

        parsed_hotel = ""
        if parsed.hotel_offer:
            parsed_hotel = parsed.hotel_offer.url
        elif parsed.hotel and parsed.hotel.urls:
            parsed_hotel = parsed.hotel.urls[0]

        flight_url = resolve_booking_url(
            "flight",
            tool_url=tool_flight,
            built_url=built_flight,
            parsed_url=parsed_flight,
        )
        hotel_url = resolve_booking_url(
            "hotel",
            tool_url=tool_hotel,
            built_url=built_hotel,
            parsed_url=parsed_hotel,
        )

        if parsed.flight_offer:
            parsed.flight_offer.url = flight_url
        elif parsed.flight:
            parsed.flight_offer = parse_flight_offer(
                parsed.flight.body,
                fallback_url=flight_url,
            )
            parsed.flight_offer.url = flight_url
        elif flight_url or self._trip_context:
            parsed.flight_offer = parse_flight_offer(
                parsed.raw or "", fallback_url=flight_url
            )
            parsed.flight_offer.url = flight_url

        if parsed.hotel_offer:
            parsed.hotel_offer.url = hotel_url
        elif parsed.hotel:
            parsed.hotel_offer = parse_hotel_offer(
                parsed.hotel.body,
                fallback_url=hotel_url,
            )
            parsed.hotel_offer.url = hotel_url
        elif hotel_url or hotel_name or self._trip_context:
            parsed.hotel_offer = parse_hotel_offer(
                parsed.raw or "", fallback_url=hotel_url
            )
            parsed.hotel_offer.url = hotel_url

        # Multi-city stays: prefer trip-context segments (always correct for
        # California-style regions). Enrich with live scrape when available.
        ctx_stays: list[dict[str, str]] = []
        if self._trip_context.get("stays"):
            for part in self._trip_context["stays"].split(";"):
                bits = part.split("|")
                if len(bits) >= 2:
                    ctx_stays.append(
                        {
                            "city": bits[0],
                            "nights": bits[1],
                            "checkin": bits[2] if len(bits) > 2 else "",
                            "checkout": bits[3] if len(bits) > 3 else "",
                            "airport": bits[4] if len(bits) > 4 else "",
                            "label": f"Stay · {bits[0]} ({bits[1]} night{'s' if bits[1] != '1' else ''})",
                            "name": f"Hotels in {bits[0]}",
                            "location": bits[0],
                            "url": "",
                            "price_label": "",
                            "total_label": "",
                            "image_url": "",
                            "score": "",
                            "score_label": "",
                            "stars": "",
                            "reviews": "",
                        }
                    )

        scraped: list[dict[str, str]] = []
        if self.agent:
            scraped = list(getattr(self.agent.browser, "last_hotel_stays", None) or [])

        stays: list[dict[str, str]] = []

        def _finalize_stay(rec: dict[str, str]) -> dict[str, str]:
            from travel_agent.browser_tools import _fallback_stay_hotel

            city = (rec.get("city") or "").strip()
            name = (rec.get("name") or "").strip()
            low = name.lower()
            bad = (
                not name
                or low.startswith("hotels in ")
                or any(
                    m in low
                    for m in ("lishui", "high speed railway", "高铁", "火车站")
                )
            )
            if bad and city:
                fb = _fallback_stay_hotel(city)
                rec["name"] = fb["name"]
                if fb.get("features"):
                    rec["features"] = fb["features"]
                if not (rec.get("image_url") or "").startswith("http") or "loremflickr" in (
                    rec.get("image_url") or ""
                ).lower():
                    rec["image_url"] = fb.get("image_url") or rec.get("image_url", "")
                if not rec.get("stars"):
                    rec["stars"] = fb.get("stars", "4")
                if not rec.get("score"):
                    rec["score"] = fb.get("score", "")
                    rec["score_label"] = fb.get("score_label", "")
                # Drop wrong-city detail links when we reject the listing name
                rec["url"] = ""
            if not (rec.get("image_url") or "").startswith("http"):
                try:
                    from travel_agent.attraction_images import lookup_image

                    rec["image_url"] = lookup_image(
                        rec.get("name") or f"{city} hotel", city=city
                    )
                except Exception:
                    pass

            # Always attach a working Trip.com link for this stay's city + dates
            checkin = (rec.get("checkin") or "").strip()
            checkout = (rec.get("checkout") or "").strip()
            raw_url = (rec.get("url") or "").strip()
            if is_trusted_hotel_detail_url(raw_url):
                rec["url"] = (
                    canonicalize_hotel_detail_url(
                        raw_url,
                        checkin=checkin,
                        checkout=checkout,
                        city=city,
                    )
                    or raw_url
                )
            elif city and checkin and checkout:
                rec["url"] = build_hotel_list_url(
                    city=city, checkin=checkin, checkout=checkout
                )
            elif not is_openable_hotel_url(raw_url):
                rec["url"] = hotel_url or raw_url
            return rec

        if len(ctx_stays) > 1:
            # Merge scrape into context stays by city name
            by_city = {
                (s.get("city") or "").strip().lower(): s for s in scraped if s.get("city")
            }
            for stub in ctx_stays:
                key = (stub.get("city") or "").strip().lower()
                live = by_city.get(key) or {}
                merged = dict(stub)
                for k in (
                    "name",
                    "url",
                    "price_label",
                    "total_label",
                    "image_url",
                    "score",
                    "score_label",
                    "stars",
                    "reviews",
                    "location",
                    "features",
                ):
                    if live.get(k):
                        merged[k] = live[k]
                # Never let scrape overwrite split nights/dates
                merged["nights"] = stub["nights"]
                merged["checkin"] = stub["checkin"]
                merged["checkout"] = stub["checkout"]
                merged["label"] = stub["label"]
                merged["city"] = stub["city"]
                stays.append(_finalize_stay(merged))
        elif scraped:
            stays = [_finalize_stay(dict(s)) for s in scraped]
        elif ctx_stays:
            stays = [_finalize_stay(dict(s)) for s in ctx_stays]

        if stays:
            from travel_agent.itinerary_parse import HotelOffer

            offers: list[HotelOffer] = []
            for stay in stays:
                off = HotelOffer(
                    name=stay.get("name") or f"Hotels in {stay.get('city', '')}",
                    location=stay.get("location") or stay.get("city", ""),
                    city=stay.get("city", ""),
                    checkin=stay.get("checkin", ""),
                    checkout=stay.get("checkout", ""),
                    nights=int(stay.get("nights") or 0) or 0,
                    stay_label=stay.get("label", ""),
                    url=stay.get("url", "") or hotel_url,
                    price_label=stay.get("price_label", ""),
                    total_label=stay.get("total_label", ""),
                    score=stay.get("score", ""),
                    score_label=stay.get("score_label", ""),
                    reviews=stay.get("reviews", ""),
                    image_url=stay.get("image_url", ""),
                    features=stay.get("features", "") or "Details on Trip.com",
                )
                try:
                    off.stars = int(stay.get("stars") or 0)
                except ValueError:
                    off.stars = 0
                offers.append(off)
            parsed.hotel_offers = offers
            parsed.hotel_offer = offers[0]

        self._enrich_flight_offer(parsed)
        # Multi-city cards already have per-city scrape data — don't overwrite
        # with a single California/region hotel from booking_links.
        if len(parsed.hotel_offers or []) <= 1:
            self._enrich_hotel_offer(parsed, hotel_name=hotel_name)
        if parsed.hotel_offers:
            parsed.hotel_offer = parsed.hotel_offers[0]
        return parsed

    def _enrich_flight_offer(self, parsed: ParsedItinerary) -> None:
        """Prefer tool-scraped structured flight fields over LLM prose."""
        from travel_agent.itinerary_parse import parse_flight_offer

        offer = parsed.flight_offer
        if not offer:
            return
        ctx = self._trip_context
        origin = to_flight_code(ctx.get("origin", "Hong Kong") or "Hong Kong").upper()
        dest = (
            (ctx.get("arrive_airport") or "").strip().upper()
            or to_flight_code(ctx.get("destination", "") or "").upper()
        )
        ret_from = (ctx.get("depart_airport") or "").strip().upper() or dest

        if self.agent:
            links = self.agent.booking_links
            airline = links.get("flight_airline", "")
            if is_plausible_airline_name(airline):
                offer.airline = airline
            if links.get("flight_depart"):
                offer.depart_time = links["flight_depart"]
            if links.get("flight_arrive"):
                offer.arrive_time = links["flight_arrive"]
            if links.get("flight_from"):
                offer.depart_airport = links["flight_from"]
            if links.get("flight_to"):
                offer.arrive_airport = links["flight_to"]
            if links.get("flight_duration") and "night" not in links["flight_duration"].lower():
                offer.duration = links["flight_duration"]
            if links.get("flight_stops"):
                offer.stops = links["flight_stops"]
            if links.get("flight_price"):
                offer.price_label = links["flight_price"]
            if links.get("flight_airline_logo"):
                offer.airline_logo = links["flight_airline_logo"]
            # Always prefer CDN logo by IATA code when we know the carrier
            cdn = airline_logo_url(offer.airline)
            if cdn:
                offer.airline_logo = cdn
            elif is_plausible_airline_name(offer.airline):
                offer.airline_logo = airline_logo_url(offer.airline)
            if links.get("flight_date"):
                offer.depart_date = links["flight_date"]
            if is_plausible_airline_name(links.get("flight_return_airline", "")):
                offer.return_airline = links["flight_return_airline"]
            if links.get("flight_return_date"):
                offer.return_date = links["flight_return_date"]
            if links.get("flight_return_depart"):
                offer.return_depart_time = links["flight_return_depart"]
            if links.get("flight_return_arrive"):
                offer.return_arrive_time = links["flight_return_arrive"]
            if links.get("flight_return_from"):
                offer.return_depart_airport = links["flight_return_from"]
            if links.get("flight_return_to"):
                offer.return_arrive_airport = links["flight_return_to"]
            if links.get("flight_return_duration") and "night" not in links[
                "flight_return_duration"
            ].lower():
                offer.return_duration = links["flight_return_duration"]
            if links.get("flight_return_stops"):
                offer.return_stops = links["flight_return_stops"]
            if links.get("flight_return_airline_logo"):
                offer.return_airline_logo = links["flight_return_airline_logo"]
            ret_cdn = airline_logo_url(offer.return_airline or "")
            if ret_cdn:
                offer.return_airline_logo = ret_cdn
            elif is_plausible_airline_name(offer.return_airline):
                offer.return_airline_logo = airline_logo_url(offer.return_airline)
            if links.get("flight_option") and (
                offer.airline in {"", "Trip.com fare"} or offer.depart_time == "--:--"
            ):
                opt = parse_flight_offer(links["flight_option"], fallback_url=offer.url)
                if offer.airline in {"", "Trip.com fare"} and is_plausible_airline_name(opt.airline):
                    offer.airline = opt.airline
                if offer.depart_time == "--:--" and opt.depart_time != "--:--":
                    offer.depart_time = opt.depart_time
                if offer.arrive_time == "--:--" and opt.arrive_time != "--:--":
                    offer.arrive_time = opt.arrive_time
                if offer.duration in {"", "—"} and opt.duration not in {"", "—"}:
                    if "night" not in opt.duration.lower():
                        offer.duration = opt.duration

        # Never keep hotel-stay phrasing in flight duration
        if offer.duration and (
            "night" in offer.duration.lower()
            or re.search(r"(?i)july|aug|sep|oct|nov|dec", offer.duration)
        ):
            offer.duration = "—"

        sparse = (
            offer.depart_time == "--:--"
            or offer.arrive_time == "--:--"
            or offer.airline in {"", "Trip.com fare"}
            or offer.price_label == "See Trip.com"
        )
        if sparse and parsed.raw:
            # Prefer the Recommended flight section only
            section = ""
            m = re.search(
                r"(?is)recommended\s+flight\b(.*?)(?:recommended\s+hotel\b|day-by-day|$)",
                parsed.raw,
            )
            if m:
                section = m.group(1)
            alt = parse_flight_offer(section or parsed.raw, fallback_url=offer.url)
            if offer.depart_time == "--:--" and alt.depart_time != "--:--":
                offer.depart_time = alt.depart_time
            if offer.arrive_time == "--:--" and alt.arrive_time != "--:--":
                offer.arrive_time = alt.arrive_time
            if offer.airline in {"", "Trip.com fare"} and is_plausible_airline_name(alt.airline):
                offer.airline = alt.airline
            if offer.price_label == "See Trip.com" and alt.price_label != "See Trip.com":
                offer.price_label = alt.price_label
            if offer.duration in {"", "—"} and alt.duration not in {"", "—"}:
                if "night" not in alt.duration.lower():
                    offer.duration = alt.duration
            if alt.stops and alt.stops != "Direct":
                offer.stops = alt.stops
            if alt.depart_airport and alt.depart_airport not in {"", "—"}:
                offer.depart_airport = alt.depart_airport
            if alt.arrive_airport and alt.arrive_airport not in {"", "—"}:
                offer.arrive_airport = alt.arrive_airport

        # Open-jaw regional trips: ALWAYS use context airports (scrape often
        # returns a same-city round-trip that must not win over the plan).
        is_open_jaw = bool(ret_from and dest and ret_from != dest)
        if is_open_jaw:
            offer.arrive_airport = dest
            offer.return_depart_airport = ret_from
            offer.return_arrive_airport = origin or offer.return_arrive_airport or "HKG"
            offer.trip_label = "Open-jaw"
            offer.badge = f"Open-jaw · {dest} in / {ret_from} out"
            if self.agent:
                self.agent.booking_links["flight_to"] = dest
                self.agent.booking_links["flight_return_from"] = ret_from
                self.agent.booking_links["flight_return_to"] = (
                    origin or offer.return_arrive_airport or "HKG"
                )
        else:
            if origin and offer.depart_airport in {"", "—", "HKG"}:
                offer.depart_airport = origin
            if dest and offer.arrive_airport in {"", "—"}:
                offer.arrive_airport = dest
            if ret_from and offer.return_depart_airport in {"", "—"}:
                offer.return_depart_airport = ret_from
            if origin and offer.return_arrive_airport in {"", "—"} and (
                offer.return_depart_time or ret_from
            ):
                offer.return_arrive_airport = origin
        # Fill dates from trip context when scraper didn't emit them
        if not offer.depart_date and ctx.get("depart_date"):
            offer.depart_date = str(ctx["depart_date"])
        if not offer.return_date and ctx.get("return_date"):
            offer.return_date = str(ctx["return_date"])
        # Friendly label when airline still unknown but we have a live fare
        if not is_plausible_airline_name(offer.airline) or offer.airline in {"", "Trip.com fare"}:
            if origin and dest:
                offer.airline = f"{origin} → {dest} flight"
            else:
                offer.airline = "Recommended flight"
        if offer.badge in {"", "Recommended"} and offer.price_label not in {"", "See Trip.com"}:
            offer.badge = "Live Trip.com fare"
        if is_plausible_airline_name(offer.airline) and not offer.airline_logo:
            offer.airline_logo = airline_logo_url(offer.airline)
        if offer.return_depart_time:
            if not is_open_jaw:
                offer.trip_label = "Round-trip"
            if not offer.return_airline and is_plausible_airline_name(offer.airline):
                offer.return_airline = offer.airline
            if is_plausible_airline_name(offer.return_airline) and not offer.return_airline_logo:
                offer.return_airline_logo = airline_logo_url(offer.return_airline)

    def _enrich_hotel_offer(self, parsed: ParsedItinerary, *, hotel_name: str = "") -> None:
        """Prefer tool-scraped hotel name/price/score over LLM prose."""
        from travel_agent.itinerary_parse import parse_hotel_offer

        offer = parsed.hotel_offer
        if not offer:
            return
        ctx = self._trip_context
        dest = to_hotel_city(ctx.get("destination", "") or "") or ctx.get("destination", "")

        def _bad_name(name: str) -> bool:
            low = (name or "").lower()
            return (
                not name
                or name in {"Recommended hotel", "Hotel"}
                or name.startswith("#")
                or "day-by-day" in low
                or "sample hotel" in low
                or "rates range" in low
                or ("option" in low and "hotel" in low and len(name) > 40)
                or len(name) > 70
            )

        if self.agent:
            links = self.agent.booking_links
            cand = links.get("hotel_name") or ""
            if cand and not _bad_name(cand):
                # Reject China rail-station hotels mapped onto Western cities
                low = cand.lower()
                if not any(
                    m in low
                    for m in ("lishui", "high speed railway", "高铁", "火车站")
                ):
                    offer.name = cand
                elif hotel_name and not _bad_name(hotel_name):
                    offer.name = hotel_name
            elif hotel_name and not _bad_name(hotel_name):
                offer.name = hotel_name
            if links.get("hotel_price"):
                offer.price_label = links["hotel_price"]
            if links.get("hotel_total"):
                offer.total_label = f"Total (incl. taxes & fees): {links['hotel_total']}"
            if links.get("hotel_location"):
                offer.location = links["hotel_location"]
            if links.get("hotel_score"):
                offer.score = links["hotel_score"]
                try:
                    val = float(offer.score)
                    offer.score_label = (
                        "Great" if val >= 9 else "Very Good" if val >= 8 else "Good"
                    )
                except ValueError:
                    offer.score_label = "Guest rating"
            if links.get("hotel_stars"):
                try:
                    offer.stars = int(links["hotel_stars"])
                except ValueError:
                    pass
            if links.get("hotel_reviews"):
                offer.reviews = links["hotel_reviews"]
            if links.get("hotel_image"):
                offer.image_url = links["hotel_image"].rstrip(".,;)")
            elif offer.url and not offer.image_url:
                # leave empty; live scrape should have filled booking_links
                pass

        if not offer.image_url:
            alt_img = re.search(
                r"(?i)(?:image|photo|cover)\s*[:\-]\s*(https?://\S+)",
                parsed.raw or "",
            )
            if alt_img:
                offer.image_url = alt_img.group(1).rstrip(".,;)")

        if _bad_name(offer.name) or offer.price_label == "See Trip.com":
            section = ""
            m = re.search(
                r"(?is)recommended\s+hotel\b(.*?)(?:day-by-day|day\s+\d+|budget|$)",
                parsed.raw or "",
            )
            if m:
                section = m.group(1)
            alt = parse_hotel_offer(section or (parsed.raw or ""), fallback_url=offer.url)
            if _bad_name(offer.name) and not _bad_name(alt.name):
                offer.name = alt.name
            if offer.price_label == "See Trip.com" and alt.price_label != "See Trip.com":
                offer.price_label = alt.price_label
            if not offer.total_label and alt.total_label:
                offer.total_label = alt.total_label
            if offer.location in {"", "See map on Trip.com"} and alt.location not in {
                "",
                "See map on Trip.com",
            }:
                offer.location = alt.location
            if offer.room_type == "Standard room" and alt.room_type != "Standard room":
                offer.room_type = alt.room_type
            if offer.score in {"", "—"} and alt.score not in {"", "—"}:
                offer.score = alt.score
                offer.score_label = alt.score_label
            if offer.stars <= 0 and alt.stars > 0:
                offer.stars = alt.stars

        if hotel_name and _bad_name(offer.name) and not _bad_name(hotel_name):
            offer.name = hotel_name

        if dest and offer.location in {"", "See map on Trip.com"}:
            offer.location = dest
        if _bad_name(offer.name):
            offer.name = f"Hotels in {dest}" if dest else "Recommended hotel"
        if offer.stars <= 0:
            offer.stars = 4
        if not offer.social_proof:
            offer.social_proof = "Live rates from Trip.com Hong Kong"

    def _apply_plan(self, text: str) -> None:
        self._last_plan = text
        parsed = self._apply_booking_urls(parse_itinerary(text))
        parsed = self._ensure_days(parsed)
        self._render_parsed(parsed)
        self._append_chat("System", "Itinerary ready. Refine below if you like.", "agent")

    def _ensure_days(self, parsed: ParsedItinerary) -> ParsedItinerary:
        """Always show Seoul-style day cards for the trip length."""
        nights = 0
        try:
            depart = self._trip_context.get("depart_date") or ""
            ret = self._trip_context.get("return_date") or ""
            if depart and ret:
                nights = max(1, (date.fromisoformat(ret) - date.fromisoformat(depart)).days)
        except ValueError:
            nights = 0
        if not nights:
            try:
                nights = max(1, int(self.nights_var.get().strip() or "0"))
            except (TypeError, ValueError, tk.TclError):
                nights = 0
        if not nights:
            nights = max(len(parsed.days), 3)
        styles = self._selected_styles() if hasattr(self, "_style_vars") else []
        dest = self._trip_context.get("destination", "") or ""

        arrive_time = ""
        return_depart_time = ""
        if self.agent:
            links = self.agent.booking_links
            arrive_time = (links.get("flight_arrive") or "").strip()
            return_depart_time = (links.get("flight_return_depart") or "").strip()
        if parsed.flight_offer:
            if not arrive_time and parsed.flight_offer.arrive_time not in {"", "--:--"}:
                arrive_time = parsed.flight_offer.arrive_time
            if not return_depart_time and parsed.flight_offer.return_depart_time not in {
                "",
                "--:--",
            }:
                return_depart_time = parsed.flight_offer.return_depart_time

        return ensure_day_blocks(
            parsed,
            nights=nights,
            destination=dest,
            styles=styles,
            arrive_time=arrive_time,
            return_depart_time=return_depart_time,
            attractions=(
                list(getattr(self.agent.browser, "last_attractions", None) or [])
                if self.agent
                else None
            ),
        )

    def _on_refine(self) -> None:
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
        refine = (
            f"{msg}\n\n"
            "Keep the same headings (Recommended flight / Recommended hotel / "
            "Day-by-day itinerary / Budget snapshot) so the board can refresh. "
            "For each Day N, use a timetable (HH:MM lines) with exact places, meals, "
            "and metro/train routes — no Transit/Go/Lunch/Also/Dinner labels. "
            "Only use https://hk.trip.com/... URLs from tools."
        )
        self._set_busy(True, "Refining itinerary…")

        def job() -> str:
            assert self.agent is not None
            answer = self.agent.chat(refine)
            try:
                self._refresh_live_booking_cards()
            except Exception:
                pass
            return answer

        self._run_browser_job(
            job,
            on_ok=lambda answer: self._apply_refine(answer),
            on_err=lambda e: self._append_chat("Error", str(e), "agent"),
            done=lambda: self._set_busy(False),
        )

    def _apply_refine(self, text: str) -> None:
        self._last_plan = text
        self._append_chat("Agent", text[:800] + ("…" if len(text) > 800 else ""), "agent")
        parsed = self._apply_booking_urls(parse_itinerary(text))
        parsed = self._ensure_days(parsed)
        self._render_parsed(parsed)

    def _append_chat(self, who: str, text: str, tag: str) -> None:
        self.chat_out.configure(state="normal")
        self.chat_out.insert("end", f"{who}\n", tag)
        pos = 0
        for match in _URL_RE.finditer(text):
            if match.start() > pos:
                self.chat_out.insert("end", text[pos : match.start()])
            url = match.group(0).rstrip(".,;")
            self.chat_out.insert("end", url, ("url", f"link:{url}"))
            pos = match.end()
        if pos < len(text):
            self.chat_out.insert("end", text[pos:])
        self.chat_out.insert("end", "\n\n")
        self.chat_out.configure(state="disabled")
        self.chat_out.see("end")

    def _on_click_url(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        widget = event.widget
        index = widget.index(f"@{event.x},{event.y}")
        for tag in widget.tag_names(index):
            if tag.startswith("link:"):
                webbrowser.open(tag[5:])
                break

    def _run_browser_job(
        self,
        job: Callable[[], Any],
        *,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[BaseException], None] | None = None,
        done: Callable[[], None] | None = None,
    ) -> None:
        """Run job on the Playwright owner thread; marshal UI callbacks back to Tk."""

        def wrapped() -> None:
            try:
                result = job()
                if on_ok is not None:
                    self.after(0, lambda r=result: on_ok(r))
            except Exception as exc:
                if on_err is not None:
                    self.after(0, lambda e=exc: on_err(e))
            finally:
                if done is not None:
                    self.after(0, done)

        self._browser_jobs.put(wrapped)

    def _boot_agent(self) -> None:
        def worker_loop() -> None:
            try:
                settings.headless = self.headless
                agent = TravelAgent(model=self.model)
                agent.start()
                self.agent = agent
                mode = "headless" if self.headless else "browser visible"
                self.after(
                    0,
                    lambda: self._set_status(
                        f"Ready · {self.model} · Trip.com HK · {mode}", C["ok"]
                    ),
                )
            except Exception as exc:
                self.after(
                    0, lambda e=exc: self._set_status(f"Browser failed: {e}", C["danger"])
                )
                self.after(
                    0,
                    lambda e=exc: messagebox.showerror(
                        "Startup error",
                        f"{e}\n\nRun: playwright install chromium",
                    ),
                )
                return

            while True:
                task = self._browser_jobs.get()
                if task is None:
                    break
                try:
                    task()
                except Exception:
                    pass

            try:
                if self.agent:
                    self.agent.close()
            except Exception:
                pass
            self.agent = None

        self._browser_thread = threading.Thread(
            target=worker_loop, daemon=True, name="voyage-browser"
        )
        self._browser_thread.start()

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.header.set_status(text, color or C["muted"])

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.generate_btn.configure(state=state)
        self.regen_btn.configure(state=state)
        if busy:
            self._set_status(label or "Working…", C["accent_deep"])
        elif self.agent:
            self._set_status(f"Ready · {self.model} · Trip.com HK", C["ok"])

    def _on_close(self) -> None:
        try:
            self._browser_jobs.put(None)
            if self._browser_thread and self._browser_thread.is_alive():
                self._browser_thread.join(timeout=5)
        except Exception:
            pass
        self.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voyage Trip.Planner-style desktop agent")
    parser.add_argument("-m", "--model", default=settings.ollama_model)
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show Playwright Chromium while scraping Trip.com (hidden by default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = TravelAgentApp(model=args.model, headless=not args.show_browser)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
