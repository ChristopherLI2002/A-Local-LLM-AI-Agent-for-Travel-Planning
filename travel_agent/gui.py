"""Trip.Planner-style desktop UI for the local LLM travel planning agent (tkinter)."""

from __future__ import annotations

import argparse
import math
import queue
import re
import threading
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from datetime import date, timedelta
from io import BytesIO
from typing import Any
from tkinter import messagebox, scrolledtext

from PIL import Image, ImageDraw, ImageTk

from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.airline_names import (
    airline_logo_url,
    airline_logo_urls,
    is_plausible_airline_name,
    split_airline_names,
)
from travel_agent.itinerary_parse import (
    CarRentalOffer,
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
    is_hotel_list_url,
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
    "btn_disabled": "#D5D9DD",
    "btn_disabled_text": "#8A9399",
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
    # Baselines are already larger than the original compact defaults
    return {
        "brand": ("Georgia", _scaled_pt(24, step), "bold"),
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
        self.create_text(
            36,
            38,
            anchor="w",
            text="A Local LLM AI Agent for Travel Planning",
            fill=C["ink"],
            font=FONT_BRAND,
        )
        self.create_text(
            40,
            78,
            anchor="w",
            text="Local Ollama planning · Trip.Planner-style itineraries · live from Trip.com Hong Kong",
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
            return C["btn_disabled"], C["line"], C["btn_disabled_text"]
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

        # Outbound search link — directly under the outbound leg
        self.out_link_row = tk.Frame(times, bg="#FFFFFF")
        self.out_link_row.pack(fill="x", pady=(8, 0))
        self.select_btn = tk.Label(
            self.out_link_row,
            text="Open outbound search",
            bg=C["trip_blue"],
            fg="#FFFFFF",
            font=FONT_UI_BOLD,
            padx=12,
            pady=5,
            cursor="hand2",
        )
        self.select_btn.pack(side="left")

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

        # Return search link — directly under the return leg
        self.ret_link_row = tk.Frame(self.return_block, bg="#FFFFFF")
        self.ret_link_row.pack(fill="x", pady=(8, 0))
        self.return_select_btn = tk.Label(
            self.ret_link_row,
            text="Open return search",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=FONT_UI_BOLD,
            padx=12,
            pady=5,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=C["trip_blue"],
            highlightcolor=C["trip_blue"],
        )
        self.return_select_btn.pack(side="left")

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

        self._url = ""
        self._return_url = ""
        self.select_btn.bind("<Button-1>", self._open)
        self.select_btn.bind(
            "<Enter>", lambda _e: self.select_btn.configure(bg=C["trip_blue_hover"])
        )
        self.select_btn.bind(
            "<Leave>", lambda _e: self.select_btn.configure(bg=C["trip_blue"])
        )
        self.return_select_btn.bind("<Button-1>", self._open_return)
        self.return_select_btn.bind(
            "<Enter>", lambda _e: self.return_select_btn.configure(fg=C["trip_blue_hover"])
        )
        self.return_select_btn.bind(
            "<Leave>", lambda _e: self.return_select_btn.configure(fg=C["trip_blue"])
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
        canvas.configure(width=sz, height=sz)
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

    def _decode_logo_tile(self, data: bytes, tile: int) -> Image.Image | None:
        try:
            if len(data) < 200:
                return None
            img = Image.open(BytesIO(data)).convert("RGBA")
            pad = max(2, tile // 10)
            box = tile - pad * 2
            scale = min(box / max(img.width, 1), box / max(img.height, 1))
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            canvas_img = Image.new("RGBA", (tile, tile), (255, 255, 255, 0))
            canvas_img.paste(
                resized,
                ((tile - new_w) // 2, (tile - new_h) // 2),
                resized,
            )
            return canvas_img
        except Exception:
            return None

    def _load_logo_photo(
        self,
        *,
        airline: str,
        logo_url: str,
        canvas: tk.Canvas,
        photo_attr: str,
    ) -> None:
        """Paint one or more airline logos (stacked like Trip.com for codeshares)."""
        names = split_airline_names(airline) or (
            [airline.strip()] if (airline or "").strip() else ["TP"]
        )
        # Collect CDN urls for every carrier; keep scraped url as last-resort for #1
        urls = airline_logo_urls(airline)
        scraped = (logo_url or "").strip()
        if scraped.startswith("http") and scraped not in urls:
            # Only append scraped if we have a single carrier and no CDN yet
            if len(urls) <= 1:
                urls = list(urls) + [scraped]

        initials = "".join(
            w[0] for w in re.sub(r"[,/&+]", " ", names[0]).split()[:2] if w
        ) or "TP"

        if not urls:
            self._draw_logo_on(canvas, initials=initials, photo_attr=photo_attr)
            return

        tile = self._LOGO_SIZE
        # Trip.com-style overlap: second logo offset down-right
        overlap = max(10, tile // 3) if len(urls) > 1 else 0
        n = min(len(urls), 3)
        canvas_w = tile + overlap * (n - 1)
        canvas_h = tile + overlap * (n - 1)
        canvas.configure(width=canvas_w, height=canvas_h)
        canvas.delete("all")

        photos: list[tk.PhotoImage] = []
        for i, url in enumerate(urls[:n]):
            try:
                data = self._fetch_logo_bytes(url)
            except Exception:
                continue
            tile_img = self._decode_logo_tile(data, tile)
            if tile_img is None:
                continue
            # White disc behind each logo so overlapping edges stay clean
            disc = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
            draw = ImageDraw.Draw(disc)
            inset = 1
            draw.ellipse(
                (inset, inset, tile - 1 - inset, tile - 1 - inset),
                fill=(255, 255, 255, 255),
                outline=(220, 226, 232, 255),
            )
            disc.alpha_composite(tile_img)
            photo = ImageTk.PhotoImage(disc)
            photos.append(photo)
            ox = i * overlap
            oy = i * overlap
            canvas.create_image(ox + tile // 2, oy + tile // 2, image=photo)

        if not photos:
            self._draw_logo_on(canvas, initials=initials, photo_attr=photo_attr)
            return

        # Keep references so Tk doesn't GC the images
        setattr(self, photo_attr, photos[0] if len(photos) == 1 else photos)

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
            logo_url=offer.return_airline_logo
            or (offer.airline_logo if not offer.return_airline else ""),
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

    def _open_return(self, _e: object | None = None) -> None:
        url = self._return_url or self._url
        if url and "trip.com" in url.lower():
            webbrowser.open(url)
        elif url:
            messagebox.showwarning("Invalid link", "No valid Trip.com return search link yet.")
        else:
            messagebox.showinfo("No link", "Generate an itinerary first to get a return search link.")

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
        self._return_url = ""
        self.select_btn.configure(text="Open outbound search")
        if self.ret_link_row.winfo_ismapped():
            self.ret_link_row.pack_forget()

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
        self.airline_lbl.configure(text=offer.airline or "See Trip.com for airline")
        self.dep_time.configure(text=offer.depart_time or "--:--")
        self.arr_time.configure(text=offer.arrive_time or "--:--")
        self.dep_airport.configure(text=offer.depart_airport or "—")
        self.arr_airport.configure(text=offer.arrive_airport or "—")
        self.duration.configure(text=offer.duration or "—")
        self.stops.configure(text=offer.stops or "Direct")
        self.price.configure(text=offer.price_label or "See Trip.com")
        self.trip_lbl.configure(text=offer.trip_label)
        self._url = offer.url
        self._return_url = getattr(offer, "return_url", "") or ""
        self.after(30, self._draw_path)

        has_return = bool(
            (offer.return_depart_time and offer.return_depart_time != "--:--")
            or (offer.return_depart_airport and offer.return_depart_airport not in {"", "—"})
            or (offer.trip_label or "").lower().startswith("open-jaw")
            or self._return_url
        )
        if has_return:
            if not self.return_block.winfo_ismapped():
                self.return_block.pack(fill="x", pady=(12, 0))
            ret_date = self._format_flight_date(offer.return_date)
            self.leg_ret_lbl.configure(
                text=f"Return · {ret_date}" if ret_date else "Return"
            )
            ret_name = offer.return_airline or (
                offer.airline
                if is_plausible_airline_name(offer.airline)
                else "See Trip.com for airline"
            )
            self.return_airline_lbl.configure(text=ret_name)
            self._set_return_airline_logo(offer)
            self.ret_dep_time.configure(text=offer.return_depart_time or "--:--")
            self.ret_arr_time.configure(text=offer.return_arrive_time or "--:--")
            self.ret_dep_airport.configure(text=offer.return_depart_airport or "—")
            self.ret_arr_airport.configure(text=offer.return_arrive_airport or "—")
            self.ret_duration.configure(text=offer.return_duration or "—")
            self.ret_stops.configure(text=offer.return_stops or "Direct")
            self.after(30, self._draw_ret_path)
            # Return search link under the return leg
            if self._return_url:
                if not self.ret_link_row.winfo_ismapped():
                    self.ret_link_row.pack(fill="x", pady=(8, 0))
                self.return_select_btn.configure(text="Open return search")
            elif self.ret_link_row.winfo_ismapped():
                self.ret_link_row.pack_forget()
            self.select_btn.configure(
                text="Open outbound search" if self._return_url else "Select"
            )
        else:
            self.return_block.pack_forget()
            self.select_btn.configure(text="Select")
            if self.ret_link_row.winfo_ismapped():
                self.ret_link_row.pack_forget()


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
            justify="left",
            wraplength=220,
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
            justify="left",
            wraplength=260,
        )
        self.location_lbl.pack(fill="x", pady=(8, 2))
        self.features_lbl = tk.Label(
            info,
            text="Highlights · —",
            bg="#FFFFFF",
            fg=C["ink_soft"],
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=260,
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
        try:
            from travel_agent.browser_tools import clean_hotel_display_name

            display_name = clean_hotel_display_name(
                offer.name or "", offer.city or ""
            ) or (offer.name or "Hotel")
        except Exception:
            display_name = offer.name or "Hotel"
        self.name_lbl.configure(text=display_name)
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
        # Prefer a city/date hotel detail URL when the offer link is missing
        url = (offer.url or "").strip()
        if is_trusted_hotel_detail_url(url):
            url = canonicalize_hotel_detail_url(
                url,
                checkin=offer.checkin or "",
                checkout=offer.checkout or "",
                city=offer.city or offer.location or "",
            ) or url
        elif not is_openable_hotel_url(url) and offer.city and offer.checkin and offer.checkout:
            url = build_hotel_list_url(
                city=offer.city,
                checkin=offer.checkin,
                checkout=offer.checkout,
            )
        self._url = url


class CarRentalRowCard(tk.Frame):
    """Trip.com-style car rental deal card (photo, specs, terms, View deal)."""

    def __init__(self, master: tk.Misc, *, heading: str = "Car rental", **kwargs) -> None:
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
        self.card = tk.Frame(shell, bg="#FFFFFF", padx=10, pady=10)
        self.card.pack(fill="x")

        top = tk.Frame(self.card, bg="#FFFFFF")
        top.pack(fill="x")

        # Left: photo + vendor + score
        left = tk.Frame(top, bg="#FFFFFF")
        left.pack(side="left", padx=(0, 10))
        self.photo = tk.Canvas(
            left, width=120, height=72, bg="#EEF2F6", highlightthickness=0
        )
        self.photo.pack()
        self._car_photo: tk.PhotoImage | None = None
        self._draw_photo_placeholder()
        self.vendor_lbl = tk.Label(
            left,
            text="",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONTS["tiny"],
            anchor="w",
            wraplength=120,
            justify="left",
        )
        self.vendor_lbl.pack(fill="x", pady=(4, 2))
        score_row = tk.Frame(left, bg="#FFFFFF")
        score_row.pack(fill="x")
        self.score_badge = tk.Label(
            score_row,
            text="—",
            bg=C["trip_blue"],
            fg="#FFFFFF",
            font=FONTS["tiny"],
            padx=5,
            pady=1,
        )
        self.score_badge.pack(side="left")
        self.reviews_lbl = tk.Label(
            score_row, text="", bg="#FFFFFF", fg=C["muted"], font=FONTS["tiny"]
        )
        self.reviews_lbl.pack(side="left", padx=(6, 0))

        # Center: name + specs + terms
        mid = tk.Frame(top, bg="#FFFFFF")
        mid.pack(side="left", fill="both", expand=True)
        title_row = tk.Frame(mid, bg="#FFFFFF")
        title_row.pack(fill="x")
        self.name_lbl = tk.Label(
            title_row,
            text="Car",
            bg="#FFFFFF",
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        )
        self.name_lbl.pack(side="left")
        self.similar_lbl = tk.Label(
            title_row,
            text="",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONT_SMALL,
            anchor="w",
        )
        self.similar_lbl.pack(side="left", padx=(6, 0))

        specs = tk.Frame(mid, bg="#FFFFFF")
        specs.pack(fill="x", pady=(4, 2))
        self.seats_lbl = tk.Label(
            specs, text="", bg="#FFFFFF", fg=C["ink"], font=FONT_SMALL
        )
        self.seats_lbl.pack(side="left", padx=(0, 10))
        self.fuel_lbl = tk.Label(
            specs, text="", bg="#FFFFFF", fg=C["ink"], font=FONT_SMALL
        )
        self.fuel_lbl.pack(side="left")

        self.pickup_lbl = tk.Label(
            mid,
            text="",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONT_SMALL,
            anchor="w",
        )
        self.pickup_lbl.pack(fill="x", pady=(2, 4))

        tk.Frame(mid, bg=C["line"], height=1).pack(fill="x", pady=(2, 4))

        self.cancel_lbl = tk.Label(
            mid, text="", bg="#FFFFFF", fg="#0A7A6A", font=FONT_SMALL, anchor="w"
        )
        self.cancel_lbl.pack(fill="x")
        self.mileage_lbl = tk.Label(
            mid, text="", bg="#FFFFFF", fg=C["ink"], font=FONT_SMALL, anchor="w"
        )
        self.mileage_lbl.pack(fill="x")
        self.payment_lbl = tk.Label(
            mid, text="", bg="#FFFFFF", fg=C["ink"], font=FONT_SMALL, anchor="w"
        )
        self.payment_lbl.pack(fill="x")
        self.insurance_lbl = tk.Label(
            mid, text="", bg="#FFFFFF", fg=C["ink"], font=FONT_SMALL, anchor="w"
        )
        self.insurance_lbl.pack(fill="x")

        # Right: price + CTA
        right = tk.Frame(top, bg="#FFFFFF")
        right.pack(side="right", padx=(8, 0))
        self.price_lbl = tk.Label(
            right,
            text="—",
            bg="#FFFFFF",
            fg=C["ink"],
            font=FONTS["price"],
            anchor="e",
        )
        self.price_lbl.pack(anchor="e")
        self.unit_lbl = tk.Label(
            right, text="/day", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL, anchor="e"
        )
        self.unit_lbl.pack(anchor="e")
        self.total_lbl = tk.Label(
            right, text="", bg="#FFFFFF", fg=C["muted"], font=FONTS["tiny"], anchor="e"
        )
        self.total_lbl.pack(anchor="e", pady=(2, 8))
        self.cta = tk.Label(
            right,
            text="View deal >",
            bg=C["trip_blue"],
            fg="#FFFFFF",
            font=FONT_UI_BOLD,
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.cta.pack(anchor="e")
        self.cta.bind("<Button-1>", self._open)
        self.cta.bind("<Enter>", lambda _e: self.cta.configure(bg=C["trip_blue_hover"]))
        self.cta.bind("<Leave>", lambda _e: self.cta.configure(bg=C["trip_blue"]))
        self._url = ""

    def _draw_photo_placeholder(self) -> None:
        self._car_photo = None
        self.photo.delete("all")
        self.photo.create_rectangle(0, 0, 120, 72, fill="#EEF2F6", outline="")
        self.photo.create_text(60, 36, text="Car", fill="#8A95A1", font=FONT_SMALL)

    def _paint_photo(self, url: str) -> None:
        try:
            raw = (url or "").strip()
            if not raw.startswith("http"):
                self._draw_photo_placeholder()
                return
            # Ctrip CDN car photos (dimg*.c-ctrip.com) need a Trip.com referer
            host = urllib.parse.urlparse(raw).netloc.lower()
            referer = "https://hk.trip.com/"
            if "c-ctrip.com" in host or "tripcdn" in host:
                referer = "https://hk.trip.com/carhire/"
            req = urllib.request.Request(
                raw,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            data = urllib.request.urlopen(req, timeout=12).read()
            if len(data) < 800:
                self._draw_photo_placeholder()
                return
            img = Image.open(BytesIO(data)).convert("RGB")
            # Reject near-black silhouette / empty CDN stubs
            sample = img.resize((32, 20))
            pixels = list(sample.getdata())
            avg = sum(sum(px) for px in pixels) / max(len(pixels) * 3, 1)
            if avg < 18:
                self._draw_photo_placeholder()
                return
            tw, th = 120, 72
            scale = max(tw / max(img.width, 1), th / max(img.height, 1))
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - tw) // 2)
            top = max(0, (new_h - th) // 2)
            fitted = resized.crop((left, top, left + tw, top + th))
            photo = ImageTk.PhotoImage(fitted)
            self._car_photo = photo
            self.photo.delete("all")
            self.photo.create_image(0, 0, image=photo, anchor="nw")
        except Exception:
            self._draw_photo_placeholder()

    def _open(self, _e: object | None = None) -> None:
        url = (self._url or "").strip()
        if url and "trip.com" in url.lower():
            webbrowser.open(url)
        elif url:
            webbrowser.open(url)
        else:
            messagebox.showinfo(
                "No link", "Generate a trip with Rent a car to get a deal link."
            )

    def set_loading(self, message: str) -> None:
        self._draw_photo_placeholder()
        self.name_lbl.configure(text=message)
        self.similar_lbl.configure(text="")
        self.vendor_lbl.configure(text="")
        self.score_badge.configure(text="—")
        self.reviews_lbl.configure(text="")
        self.seats_lbl.configure(text="")
        self.fuel_lbl.configure(text="")
        self.pickup_lbl.configure(text="Searching Trip.com car hire…")
        self.cancel_lbl.configure(text="")
        self.mileage_lbl.configure(text="")
        self.payment_lbl.configure(text="")
        self.insurance_lbl.configure(text="")
        self.price_lbl.configure(text="…")
        self.total_lbl.configure(text="")
        self._url = ""

    def set_offer(self, offer: CarRentalOffer) -> None:
        loc = offer.location or ""
        heading = "Car rental"
        if loc:
            heading = f"Car rental · {loc}"
        self.heading_lbl.configure(text=heading)
        name = (offer.name or "").strip() or "Recommended car"
        similar = (offer.similar or "").strip()
        # Trip.com title style: "Dodge Charger or similar Standard car"
        if similar and similar.lower() not in name.lower():
            self.name_lbl.configure(text=name)
            self.similar_lbl.configure(text=similar)
        elif " or similar" in name.lower():
            parts = re.split(r"(?i)\s+(or similar\b.*)$", name, maxsplit=1)
            self.name_lbl.configure(text=(parts[0] or name).strip())
            self.similar_lbl.configure(
                text=(parts[1].strip() if len(parts) > 1 else "")
            )
        else:
            self.name_lbl.configure(text=name)
            self.similar_lbl.configure(text=similar)
        self.vendor_lbl.configure(text=offer.vendor or "")
        self.score_badge.configure(text=offer.score or "—")
        self.reviews_lbl.configure(text=offer.reviews or "")
        seats = ""
        if offer.seats:
            seats = (
                offer.seats
                if "seat" in offer.seats.lower()
                else f"{offer.seats} seats"
            )
        self.seats_lbl.configure(text=seats)
        self.fuel_lbl.configure(text=offer.fuel or "")
        self.pickup_lbl.configure(text=offer.pickup_note or "")
        self.cancel_lbl.configure(text=offer.cancellation or "")
        self.mileage_lbl.configure(text=offer.mileage or "")
        self.payment_lbl.configure(text=offer.payment or "")
        self.insurance_lbl.configure(text=offer.insurance or "")
        self.price_lbl.configure(text=offer.price_label or "See Trip.com")
        self.unit_lbl.configure(text=offer.price_unit or "/day")
        self.total_lbl.configure(text=offer.total_label or "")
        url = (offer.url or "").strip()
        self._url = url
        if (offer.image_url or "").startswith("http"):
            self._paint_photo(offer.image_url)
        else:
            self._draw_photo_placeholder()


class TravelAgentApp(tk.Tk):
    def __init__(self, model: str, headless: bool = True) -> None:
        super().__init__()
        self.model = model
        # Default: scrape Trip.com without a visible Chromium window
        self.headless = headless
        self.agent: TravelAgent | None = None
        self._busy = False
        self._ready = False
        self._wizard_step = 1
        self._last_plan = ""
        self._style_vars: dict[str, tk.BooleanVar] = {}
        self._trip_context: dict[str, str] = {}
        self._live_flight_card: dict[str, str] = {}
        self._live_flight_error: str = ""
        # Playwright sync API is thread-bound: one long-lived worker owns the browser.
        self._browser_jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._browser_thread: threading.Thread | None = None
        self._timetable_thumb_cache: dict[str, tk.PhotoImage] = {}
        self._font_step = DEFAULT_FONT_STEP
        apply_font_globals(self._font_step)

        self.title("A Local LLM AI Agent for Travel Planning")
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
        self._set_status("Preparing… starting Ollama and browser", C["muted"])
        self._sync_action_buttons(preparing_label=True)
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
            text="Destination, dates, and style on one page — then the local LLM builds flights, hotels, and a day-by-day plan.",
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

        self.rent_car_var = tk.BooleanVar(value=False)
        Chip(form, "Rent a car", self.rent_car_var).pack(anchor="w", pady=(0, 16))

        row = tk.Frame(form, bg=C["paper"])
        row.pack(fill="x")
        self.generate_btn = PillButton(
            row, "Generate itinerary", command=self._on_generate, width=200
        )
        self.generate_btn.pack(side="left")
        # Disabled (gray) until Ollama + Playwright finish booting
        self.generate_btn.configure(state="disabled")
        self.generate_btn.configure(text="Preparing…")

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
        self.regen_btn.configure(state="disabled")

        split = tk.Frame(self.results, bg=C["paper"])
        split.pack(fill="x", anchor="n")
        # Narrow bookings column; itinerary takes the rest
        split.columnconfigure(0, weight=0, minsize=280)
        split.columnconfigure(1, weight=1, minsize=480)

        left = tk.Frame(split, bg=C["paper"])
        left.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        self._bookings_col = left
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
        self.car_row = CarRentalRowCard(left)
        # Hidden until a rental is needed / scraped
        # self.car_row.pack(...) when offer exists

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

    def _render_car_card(self, parsed: ParsedItinerary) -> None:
        """Show the car rental card only when Rent a car was selected."""
        want = bool(self._trip_context.get("rent_car"))
        if not want:
            parsed.car_offer = None
            if self.car_row.winfo_ismapped():
                self.car_row.pack_forget()
            return
        offer = parsed.car_offer
        useful = bool(
            offer
            and self._car_card_useful(
                {
                    "name": offer.name,
                    "url": offer.url,
                    "price_label": offer.price_label,
                    "vendor": offer.vendor,
                }
            )
        )
        if useful and offer:
            if not self.car_row.winfo_ismapped():
                self.car_row.pack(fill="x", pady=(4, 8))
            self.car_row.set_offer(offer)
        else:
            if not self.car_row.winfo_ismapped():
                self.car_row.pack(fill="x", pady=(4, 8))
            self.car_row.set_loading("No car deal scraped yet")

    def _car_offer_from_live_card(self, card: dict[str, str]) -> CarRentalOffer:
        return CarRentalOffer(
            name=card.get("name", "") or card.get("car_name", ""),
            similar=card.get("similar", "") or card.get("car_similar", ""),
            image_url=card.get("image_url", "") or card.get("car_image", ""),
            vendor=card.get("vendor", "") or card.get("car_vendor", ""),
            score=card.get("score", "") or card.get("car_score", ""),
            reviews=card.get("reviews", "") or card.get("car_reviews", ""),
            seats=card.get("seats", "") or card.get("car_seats", ""),
            fuel=card.get("fuel", "") or card.get("car_fuel", ""),
            pickup_note=card.get("pickup_note", "") or card.get("car_pickup_note", ""),
            cancellation=card.get("cancellation", "") or card.get("car_cancellation", ""),
            mileage=card.get("mileage", "") or card.get("car_mileage", ""),
            payment=card.get("payment", "") or card.get("car_payment", ""),
            insurance=card.get("insurance", "") or card.get("car_insurance", ""),
            price_label=card.get("price_label", "") or card.get("car_price", ""),
            price_unit="/day",
            total_label=card.get("total_label", "") or card.get("car_total", ""),
            url=card.get("url", "") or card.get("car", ""),
            location=card.get("location", "") or card.get("car_location", ""),
            pickup_date=card.get("pickup_date", "") or card.get("car_pickup", ""),
            dropoff_date=card.get("dropoff_date", "") or card.get("car_dropoff", ""),
        )

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
        self._render_car_card(parsed)

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
        if self._busy or not self._ready:
            return
        if not self.agent:
            messagebox.showwarning(
                "Not ready",
                "Still preparing Ollama and the Trip.com browser. Please wait.",
            )
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
            rent_car=bool(getattr(self, "rent_car_var", None) and self.rent_car_var.get()),
        )
        self._trip_context = {
            "origin": self.origin_var.get().strip() or "Hong Kong",
            "destination": destination,
            "depart_date": depart,
            "return_date": ret or "",
            "nights": str(nights),
            "rent_car": "1" if getattr(self, "rent_car_var", None) and self.rent_car_var.get() else "",
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
            self.agent.clear_booking_links()
            self.agent.force_rent_car = bool(self._trip_context.get("rent_car"))
            style_line = ", ".join(styles)
            self.agent.browser._update_selection_context(
                origin=self.origin_var.get().strip() or "Hong Kong",
                destination=destination,
                budget_hkd=budget,
                travel_styles=style_line,
                interests=style_line,
                nights=nights,
                checkin=depart,
                checkout=ret or "",
                rent_car=bool(self._trip_context.get("rent_car")),
                pickup_date=depart,
                dropoff_date=ret or "",
            )
            self.agent.browser.last_proposed_route = None
            self.agent.browser.last_flight_card = {}
            self.agent.browser.last_plan_flight_card = {}
            self.agent.browser.last_hotel_stays = []
            self.agent.browser.last_car_card = {}
            self.agent.browser.last_car_detail_url = ""
            self.agent.browser.last_hotel_name = ""
            self.agent.browser.last_hotel_detail_url = ""
            self.agent.browser.last_hotel_list_url = ""
            self.agent.browser.last_attraction_day_plan = []
            self.agent.browser.last_attraction_cards = []
            self.agent.browser.last_attractions = []
            if not self._trip_context.get("rent_car"):
                # Ensure no leftover car deal can paint the card
                self.agent.browser.last_car_card = {}
                self.agent.browser.last_car_detail_url = ""
        self._live_flight_card = {}
        self._live_flight_error = ""

        self._show_results()
        self.flight_row.set_loading("Comparing flights on Trip.com…")
        self.hotel_row.set_loading("Comparing hotels on Trip.com…")
        if self.car_row.winfo_ismapped():
            self.car_row.pack_forget()
        if self._trip_context.get("rent_car"):
            if not self.car_row.winfo_ismapped():
                self.car_row.pack(fill="x", pady=(4, 8))
            self.car_row.set_loading("Comparing car rentals on Trip.com…")
        elif self.car_row.winfo_ismapped():
            self.car_row.pack_forget()
        self._clear_days()
        loading = tk.Frame(self.days_inner, bg="#FFFFFF", padx=14, pady=16)
        loading.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        tk.Label(
            loading,
            text="Building your itinerary… status updates appear at the top.",
            bg="#FFFFFF",
            fg=C["muted"],
            font=FONT_BODY,
            anchor="w",
        ).pack(anchor="w")
        self._set_busy(True, "Building Trip.Planner-style itinerary…")

        def job() -> str:
            assert self.agent is not None
            self.agent.force_rent_car = bool(self._trip_context.get("rent_car"))
            answer = self.agent.chat(query)
            # Guarantee open-jaw times exist before the UI paints the card
            try:
                self._ensure_open_jaw_flight_card()
            except Exception as exc:
                self._live_flight_error = str(exc)
            try:
                self._refresh_live_booking_cards()
            except Exception:
                pass
            # Snapshot for the main-thread UI paint (do not rely only on browser state)
            try:
                plan = (
                    getattr(self.agent.browser, "last_plan_flight_card", None) or {}
                )
                # Only overwrite if the scrape actually has times (URLs alone are not enough)
                if plan.get("flight_depart"):
                    self._live_flight_card = dict(plan)
                elif not getattr(self, "_live_flight_card", {}).get("flight_depart") and plan:
                    # Keep URLs for buttons, but do not pretend times exist
                    merged = dict(getattr(self, "_live_flight_card", None) or {})
                    for k, v in plan.items():
                        if v and (k in {"flight", "flight_return"} or not merged.get(k)):
                            merged[k] = v
                    self._live_flight_card = merged
            except Exception:
                pass
            return answer

        self._run_browser_job(
            job,
            on_ok=lambda answer: self._apply_plan(answer),
            on_err=lambda e: self._apply_plan(f"Error: {e}"),
            done=lambda: self._set_busy(False),
        )

    def _ensure_open_jaw_flight_card(self) -> None:
        """Scrape open-jaw legs when the flight card would otherwise show --:--."""
        if not self.agent:
            return
        ctx = self._trip_context
        arrive = (ctx.get("arrive_airport") or "").strip().upper()
        ret_from = (ctx.get("depart_airport") or "").strip().upper()
        route = getattr(self.agent.browser, "last_proposed_route", None)
        if route is not None:
            ra = (getattr(route, "arrive_airport", "") or "").strip().upper()
            rd = (getattr(route, "depart_airport", "") or "").strip().upper()
            if ra:
                arrive = ra
                ctx["arrive_airport"] = ra
            if rd:
                ret_from = rd
                ctx["depart_airport"] = rd
        # Last resort: rebuild regional route from destination
        if not (arrive and ret_from and arrive != ret_from):
            try:
                from travel_agent.regions import build_regional_route

                nights = 7
                try:
                    d0 = ctx.get("depart_date") or ""
                    d1 = ctx.get("return_date") or ""
                    if d0 and d1:
                        nights = max(
                            1,
                            (date.fromisoformat(d1) - date.fromisoformat(d0)).days,
                        )
                except ValueError:
                    pass
                built = build_regional_route(
                    ctx.get("destination", "") or "",
                    nights,
                    depart_date=ctx.get("depart_date") or None,
                )
                if built:
                    arrive = (built.arrive_airport or "").upper()
                    ret_from = (built.depart_airport or "").upper()
                    ctx["arrive_airport"] = arrive
                    ctx["depart_airport"] = ret_from
            except Exception:
                pass
        if not (arrive and ret_from and arrive != ret_from):
            return
        depart = ctx.get("depart_date") or ""
        ret_date = ctx.get("return_date") or ""
        if not depart or not ret_date:
            return

        plan = getattr(self.agent.browser, "last_plan_flight_card", None) or {}
        # Reuse only when both legs have real clock times (not empty placeholders)
        time_ok = re.fullmatch(r"[0-2]?\d:[0-5]\d", (plan.get("flight_depart") or "").strip())
        ret_ok = re.fullmatch(
            r"[0-2]?\d:[0-5]\d", (plan.get("flight_return_depart") or "").strip()
        )
        complete = bool(
            time_ok
            and ret_ok
            and plan.get("flight_airline")
            and (plan.get("flight") or self.agent.booking_links.get("flight"))
            and (plan.get("flight_return") or self.agent.booking_links.get("flight_return"))
        )
        if complete:
            self._merge_live_flight(
                {k: v for k, v in plan.items() if k.startswith("flight") and v}
            )
            self._live_flight_card = {
                k: v for k, v in plan.items() if k.startswith("flight") and v
            }
            return

        # Always open two Trip.com one-way search pages and pick top fares
        self.after(
            0,
            lambda: self._set_status(
                f"Open-jaw: searching {arrive} outbound + {ret_from} return…",
                C["accent_deep"],
            ),
        )
        oj = self.agent.browser.search_open_jaw_flights(
            origin=ctx.get("origin", "Hong Kong") or "Hong Kong",
            arrive_airport=arrive,
            return_airport=ret_from,
            depart_date=depart,
            return_date=ret_date,
            adults=1,
        )
        self._merge_live_flight(oj)
        self._live_flight_card = dict(oj)
        if not oj.get("flight_depart"):
            raise RuntimeError(
                f"Trip.com returned no outbound times for {arrive} "
                f"(check network / try again)"
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

        # Airports often live on the proposed route before _apply_plan copies them
        # into trip context — without this, open-jaw force-scrape never runs and
        # the card stays at --:-- while the badge still shows SFO/SAN later.
        route = getattr(self.agent.browser, "last_proposed_route", None)
        if route is not None:
            ra = (getattr(route, "arrive_airport", "") or "").strip().upper()
            rd = (getattr(route, "depart_airport", "") or "").strip().upper()
            if ra:
                arrive = ra
                ctx["arrive_airport"] = ra
            if rd:
                ret_from = rd
                ctx["depart_airport"] = rd
            if getattr(route, "stays", None) and not stays_raw:
                try:
                    ctx["stays"] = ";".join(
                        f"{s.city}|{s.nights}|{s.checkin}|{s.checkout}|{s.airport}"
                        for s in route.stays
                    )
                    hotel_city = route.stays[0].city
                    if route.stays[0].checkin:
                        hotel_checkin = route.stays[0].checkin
                    if route.stays[0].checkout:
                        hotel_checkout = route.stays[0].checkout
                except Exception:
                    pass

        open_jaw = bool(arrive and ret_from and arrive != ret_from)

        # Hotel photo/details first — flight goto must not discard hotel image work
        try:
            live_detail = (
                getattr(self.agent.browser, "last_hotel_detail_url", "") or ""
            ).strip()
            if live_detail and is_trusted_hotel_detail_url(live_detail):
                self.agent.booking_links["hotel"] = live_detail
            need_detail = not is_trusted_hotel_detail_url(
                self.agent.booking_links.get("hotel", "")
            )
            if need_detail and hotel_city and hotel_checkin:
                live = fetch_hotel_detail_link(
                    self.agent.browser,
                    city=hotel_city,
                    checkin=hotel_checkin,
                    checkout=hotel_checkout or checkout,
                )
                self._merge_live_hotel(live)
            # Prefer Playwright detail again after fetch
            live_detail = (
                getattr(self.agent.browser, "last_hotel_detail_url", "") or ""
            ).strip()
            if live_detail and is_trusted_hotel_detail_url(live_detail):
                self.agent.booking_links["hotel"] = live_detail
            self._ensure_hotel_image()
        except Exception:
            pass

        # Guarantee car hire scrape when Rent a car was selected
        try:
            if ctx.get("rent_car"):
                card = dict(getattr(self.agent.browser, "last_car_card", None) or {})
                if self.agent.booking_links.get("car") and not card.get("url"):
                    card["url"] = self.agent.booking_links["car"]
                if not self._car_card_useful(card):
                    pickup = hotel_city or to_hotel_city(
                        ctx.get("destination", "") or ""
                    )
                    if pickup:
                        self.after(
                            0,
                            lambda: self._set_status(
                                "Fetching car rental deal from Trip.com…",
                                C["accent_deep"],
                            ),
                        )
                        self.agent.browser.search_cars(
                            location=pickup,
                            pickup_date=ctx.get("depart_date") or "",
                            dropoff_date=ctx.get("return_date") or checkout,
                        )
                        self.agent._prefer_live_car_card()
        except Exception:
            pass

        try:
            links = self.agent.booking_links
            # Always re-apply frozen plan card first
            plan_card = (
                getattr(self.agent.browser, "last_plan_flight_card", None) or {}
            )
            for k, v in plan_card.items():
                if v and k.startswith("flight"):
                    cur = links.get(k) or ""
                    if not cur or cur in {"--:--", "See Trip.com", "n/a"}:
                        links[k] = v
                    elif k in {
                        "flight_depart",
                        "flight_arrive",
                        "flight_airline",
                        "flight_return_depart",
                        "flight_return_arrive",
                        "flight_return_airline",
                        "flight_price",
                    }:
                        links[k] = v

            if open_jaw:
                links["flight_to"] = arrive
                links["flight_return_from"] = ret_from
                links["flight_return_to"] = to_flight_code(
                    ctx.get("origin", "Hong Kong") or "Hong Kong"
                ).upper() or "HKG"
                if ctx.get("return_date"):
                    links["flight_return_date"] = ctx["return_date"]
                if ctx.get("depart_date"):
                    links.setdefault("flight_date", ctx["depart_date"])

                # Missing times/airline/price → scrape both open-jaw legs now
                sparse = not links.get("flight_depart") or not links.get(
                    "flight_airline"
                )
                if sparse and ctx.get("depart_date") and ctx.get("return_date"):
                    self.after(
                        0,
                        lambda: self._set_status(
                            "Fetching open-jaw flight times from Trip.com…",
                            C["accent_deep"],
                        ),
                    )
                    oj = self.agent.browser.search_open_jaw_flights(
                        origin=ctx.get("origin", "Hong Kong") or "Hong Kong",
                        arrive_airport=arrive,
                        return_airport=ret_from,
                        depart_date=ctx["depart_date"],
                        return_date=ctx["return_date"],
                        adults=1,
                    )
                    self._merge_live_flight(oj)
            elif ctx.get("origin") and ctx.get("destination") and ctx.get("depart_date"):
                if not (
                    self.agent.booking_links.get("flight_airline")
                    or self.agent.booking_links.get("flight_depart")
                ):
                    dest_code = arrive or to_flight_code(ctx["destination"]).upper()
                    live_flight = fetch_flight_card(
                        self.agent.browser,
                        origin=ctx["origin"],
                        destination=dest_code or ctx["destination"],
                        depart_date=ctx["depart_date"],
                        return_date=ctx.get("return_date") or checkout,
                    )
                    self._merge_live_flight(live_flight)
        except Exception as exc:
            # Keep going — itinerary text still renders — but surface why card is empty
            try:
                self.after(
                    0,
                    lambda e=exc: self._set_status(
                        f"Flight card refresh failed: {e}", C["danger"]
                    ),
                )
            except Exception:
                pass

    def _merge_live_hotel(self, live: dict[str, str]) -> None:
        """Copy scraped hotel detail fields into agent.booking_links."""
        if not self.agent:
            return
        url = (live.get("url") or "").strip()
        if url and is_trusted_hotel_detail_url(url):
            self.agent.booking_links["hotel"] = url
        elif url and not is_trusted_hotel_detail_url(
            self.agent.booking_links.get("hotel", "")
        ):
            # Keep list only as last resort when no detail exists yet
            self.agent.booking_links["hotel"] = url
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
        if live.get("flight_return"):
            self.agent.booking_links["flight_return"] = live["flight_return"]
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
            "flight_outbound_price",
            "flight_return_price",
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
        if trip_type == "oneway" and ret_from and ret:
            ret_url = build_flight_search_url(
                origin=ret_from,
                destination=ctx.get("origin", "Hong Kong"),
                depart_date=ret,
                trip_type="oneway",
            )
            if self.agent and not self.agent.booking_links.get("flight_return"):
                self.agent.booking_links["flight_return"] = ret_url
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
        # Prefer live Playwright detail URL; list only if no hotelId page
        hotel = ""
        if self.agent:
            hotel = (
                getattr(self.agent.browser, "last_hotel_detail_url", "") or ""
            ).strip()
            if not hotel:
                hotel = (self.agent.booking_links.get("hotel") or "").strip()
            if not hotel:
                hotel = (
                    getattr(self.agent.browser, "last_hotel_list_url", "") or ""
                ).strip()
        if not hotel or (
            "/hotels/detail" not in hotel.lower()
            and "hotelid=" not in hotel.lower()
            and "/hotels/list" not in hotel.lower()
        ):
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
        # Multi-city: each stay already has its own scrape — do not stamp the
        # global last_hotel_* (one city) onto every card.
        multi_stay = len(ctx_stays) > 1 or len(scraped) > 1

        def _finalize_stay(rec: dict[str, str]) -> dict[str, str]:
            from travel_agent.browser_tools import (
                _fallback_stay_hotel,
                _is_curated_fallback_name,
                clean_hotel_display_name,
            )

            def hotelId_match(a: str, b: str) -> bool:
                ma = re.search(r"hotelId=(\d+)", a or "", re.I)
                mb = re.search(r"hotelId=(\d+)", b or "", re.I)
                return bool(ma and mb and ma.group(1) == mb.group(1))

            city = (rec.get("city") or "").strip()
            name = (rec.get("name") or "").strip()
            low = name.lower()
            china_wrong = any(
                m in low
                for m in ("lishui", "high speed railway", "高铁", "火车站")
            )
            generic = (
                not name
                or low.startswith("hotels in ")
                or low.startswith("recommended hotel")
                or _is_curated_fallback_name(name, city)
            )
            live_name = ""
            live_detail = ""
            live_list = ""
            if self.agent and not multi_stay:
                live_name = (
                    getattr(self.agent.browser, "last_hotel_name", "") or ""
                ).strip()
                live_detail = (
                    getattr(self.agent.browser, "last_hotel_detail_url", "") or ""
                ).strip()
                live_list = (
                    getattr(self.agent.browser, "last_hotel_list_url", "") or ""
                ).strip()
                if not live_name:
                    live_name = (self.agent.booking_links.get("hotel_name") or "").strip()
                live_name = clean_hotel_display_name(live_name, city)

            # Single-stay only: prefer the live Trip.com detail title
            if (
                live_name
                and not live_name.lower().startswith(
                    ("hotels in ", "recommended hotel")
                )
                and not _is_curated_fallback_name(live_name, city)
            ):
                rec["name"] = live_name
                name = live_name
                low = name.lower()
                generic = False
                china_wrong = any(
                    m in low
                    for m in ("lishui", "high speed railway", "高铁", "火车站")
                )
            elif name:
                cleaned = clean_hotel_display_name(name, city)
                if cleaned:
                    rec["name"] = cleaned
                    name = cleaned
                    low = name.lower()

            saved_url = (rec.get("url") or "").strip()
            has_detail = is_trusted_hotel_detail_url(saved_url)
            # Never paint a curated city stub (e.g. Hotel del Coronado) over a
            # real Trip.com detail link — that is how link≠name mismatches happen.
            if (generic or china_wrong) and city and not has_detail:
                fb = _fallback_stay_hotel(city)
                # Only replace with curated fallback when we still lack a real name
                if generic and not live_name:
                    curated = (fb.get("name") or "").strip()
                    if curated and not curated.lower().startswith("recommended hotel"):
                        rec["name"] = curated
                    if fb.get("features") and not rec.get("features"):
                        rec["features"] = fb["features"]
                if china_wrong:
                    # Drop wrong-region detail links only
                    saved_url = ""
                    rec["url"] = ""
                if not (rec.get("image_url") or "").startswith("http") or "loremflickr" in (
                    rec.get("image_url") or ""
                ).lower():
                    if fb.get("image_url"):
                        rec["image_url"] = fb["image_url"]
                if not rec.get("stars"):
                    rec["stars"] = fb.get("stars", "4")
                if not rec.get("score"):
                    rec["score"] = fb.get("score", "")
                    rec["score_label"] = fb.get("score_label", "")
            elif has_detail and _is_curated_fallback_name(rec.get("name") or "", city):
                # Drop curated stub names when the URL already points at a real hotel
                if live_name and not _is_curated_fallback_name(live_name, city):
                    rec["name"] = live_name
                else:
                    for s in scraped:
                        su = (s.get("url") or "").strip()
                        if su and hotelId_match(su, saved_url) and s.get("name"):
                            if not _is_curated_fallback_name(s["name"], city):
                                rec["name"] = clean_hotel_display_name(
                                    s["name"], city
                                )
                                break
            if has_detail:
                # Drop curated highlight text that belongs to a different stub hotel
                fb_feats = (_fallback_stay_hotel(city).get("features") or "").strip()
                if fb_feats and (rec.get("features") or "").strip() == fb_feats:
                    rec["features"] = ""
                # Detail URL is authoritative — always refresh name/photo/score from
                # Trip.com detail HTML for multi-city (avoids first-list-card mismatch).
                need_meta = (
                    multi_stay
                    or generic
                    or china_wrong
                    or _is_curated_fallback_name(rec.get("name") or "", city)
                    or not (rec.get("image_url") or "").startswith("http")
                    or "loremflickr" in (rec.get("image_url") or "").lower()
                    or not (rec.get("score") or "").strip()
                )
                if need_meta and saved_url:
                    try:
                        from travel_agent.browser_tools import (
                            _http_fetch_hotel_detail_meta,
                        )

                        meta = _http_fetch_hotel_detail_meta(saved_url)
                    except Exception:
                        meta = {}
                    if meta.get("name") and not _is_curated_fallback_name(
                        meta["name"], city
                    ):
                        rec["name"] = meta["name"]
                        name = meta["name"]
                        low = name.lower()
                        generic = False
                    if meta.get("image_url"):
                        # Always replace stock/wrong photos when we have the
                        # official cover for this hotelId
                        cur_img = (rec.get("image_url") or "").lower()
                        if (
                            multi_stay
                            or not cur_img.startswith("http")
                            or "loremflickr" in cur_img
                            or "unsplash" in cur_img
                        ):
                            rec["image_url"] = meta["image_url"]
                    for k in (
                        "score",
                        "score_label",
                        "reviews",
                        "stars",
                        "location",
                        "price_label",
                    ):
                        if meta.get(k) and (multi_stay or not rec.get(k)):
                            rec[k] = meta[k]

            if not (rec.get("image_url") or "").startswith("http"):
                try:
                    from travel_agent.attraction_images import lookup_image

                    rec["image_url"] = lookup_image(
                        rec.get("name") or f"{city} hotel", city=city
                    )
                except Exception:
                    pass

            # Prefer Playwright hotel detail; never wipe a good detail URL for a generic name
            checkin = (rec.get("checkin") or "").strip()
            checkout = (rec.get("checkout") or "").strip()
            raw_url = (rec.get("url") or saved_url or "").strip()
            booking_hotel = ""
            if self.agent and not multi_stay:
                booking_hotel = (self.agent.booking_links.get("hotel") or "").strip()
            # Per-stay URL first when multi-city; else live detail wins
            url_candidates = (
                (raw_url, live_detail, booking_hotel)
                if multi_stay
                else (live_detail, booking_hotel, raw_url)
            )

            for cand in url_candidates:
                if cand and is_trusted_hotel_detail_url(cand):
                    rec["url"] = (
                        canonicalize_hotel_detail_url(
                            cand,
                            checkin=checkin,
                            checkout=checkout,
                            city=city,
                        )
                        or cand
                    )
                    n = (rec.get("name") or "").strip()
                    if (
                        not n
                        or n.lower().startswith(("hotels in ", "recommended hotel"))
                        or _is_curated_fallback_name(n, city)
                    ):
                        if live_name and not _is_curated_fallback_name(live_name, city):
                            rec["name"] = live_name
                        else:
                            for s in scraped:
                                if hotelId_match(s.get("url") or "", cand) and s.get(
                                    "name"
                                ):
                                    if not _is_curated_fallback_name(s["name"], city):
                                        rec["name"] = clean_hotel_display_name(
                                            s["name"], city
                                        )
                                        break
                    return rec
            if raw_url and is_hotel_list_url(raw_url) and (
                "cityid=" in raw_url.lower() or "city=" in raw_url.lower()
            ):
                rec["url"] = raw_url
            elif live_list and is_hotel_list_url(live_list) and (
                "cityid=" in live_list.lower() or "city=" in live_list.lower()
            ):
                rec["url"] = live_list
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
            # Single-city scrape often inherits a default 7-night stay — expand
            # to the wizard trip length when needed.
            try:
                want_n = max(1, int(self._trip_context.get("nights") or 0))
            except ValueError:
                want_n = 0
            if len(stays) == 1 and want_n:
                try:
                    have_n = int(stays[0].get("nights") or 0)
                except ValueError:
                    have_n = 0
                if have_n < want_n:
                    stays[0]["nights"] = str(want_n)
                    cin = (stays[0].get("checkin") or self._trip_context.get("depart_date") or "").strip()
                    cout = (self._trip_context.get("return_date") or "").strip()
                    if cin and not cout:
                        try:
                            cout = (
                                date.fromisoformat(cin) + timedelta(days=want_n)
                            ).isoformat()
                        except ValueError:
                            cout = stays[0].get("checkout") or ""
                    if cin:
                        stays[0]["checkin"] = cin
                    if cout:
                        stays[0]["checkout"] = cout
                    stays[0]["label"] = (
                        f"Stay · {stays[0].get('city') or ''} "
                        f"({want_n} night{'s' if want_n != 1 else ''})"
                    ).strip()
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
        self._enrich_car_offer(parsed)
        return parsed

    def _car_card_useful(self, card: dict[str, str]) -> bool:
        """True when scrape/booking_links have a real deal (not a loading stub)."""
        name = (card.get("name") or card.get("car_name") or "").strip()
        low = name.lower()
        if low.startswith("searching") or low in {"car", "recommended car"}:
            name_ok = False
        else:
            name_ok = bool(name)
        url = (card.get("url") or card.get("car") or "").strip()
        detail_ok = "/carrentals/detail" in url.lower()
        price = (card.get("price_label") or card.get("car_price") or "").strip()
        price_ok = bool(price) and price not in {"See Trip.com", "…", "—"}
        return bool(detail_ok or price_ok or (name_ok and (url or card.get("vendor"))))

    def _enrich_car_offer(self, parsed: ParsedItinerary) -> None:
        """Prefer Playwright carhire scrape for the left-column car card."""
        if not self._trip_context.get("rent_car"):
            parsed.car_offer = None
            return
        live: dict[str, str] = {}
        if self.agent:
            live = dict(getattr(self.agent.browser, "last_car_card", None) or {})
            if self.agent.booking_links.get("car") and not live.get("url"):
                live["url"] = self.agent.booking_links["car"]
            # Overlay booking_links fields when present
            for src, dest in (
                ("car_name", "name"),
                ("car_similar", "similar"),
                ("car_vendor", "vendor"),
                ("car_score", "score"),
                ("car_reviews", "reviews"),
                ("car_seats", "seats"),
                ("car_fuel", "fuel"),
                ("car_pickup_note", "pickup_note"),
                ("car_cancellation", "cancellation"),
                ("car_mileage", "mileage"),
                ("car_payment", "payment"),
                ("car_insurance", "insurance"),
                ("car_price", "price_label"),
                ("car_total", "total_label"),
                ("car_image", "image_url"),
                ("car_location", "location"),
                ("car_pickup", "pickup_date"),
                ("car_dropoff", "dropoff_date"),
                ("car", "url"),
            ):
                val = self.agent.booking_links.get(src) or ""
                if val and not live.get(dest):
                    live[dest] = val
        if self._car_card_useful(live):
            parsed.car_offer = self._car_offer_from_live_card(live)
        else:
            # Keep loading state in the UI — do not paint a fake "Searching…" deal
            parsed.car_offer = None

    def _enrich_flight_offer(self, parsed: ParsedItinerary) -> None:
        """Prefer tool-scraped structured flight fields over LLM prose."""
        from travel_agent.itinerary_parse import parse_flight_offer

        offer = parsed.flight_offer
        if not offer:
            return
        ctx = self._trip_context

        def _iata(raw: str, *, fallback: str = "") -> str:
            """Force airports to 3-letter IATA (never 'FLORIDA' / city names)."""
            code = to_flight_code((raw or "").strip())
            if code and re.fullmatch(r"[A-Za-z]{3}", code):
                return code.upper()
            fb = to_flight_code((fallback or "").strip())
            if fb and re.fullmatch(r"[A-Za-z]{3}", fb):
                return fb.upper()
            only = re.sub(r"[^A-Za-z]", "", (raw or ""))
            if re.fullmatch(r"[A-Za-z]{3}", only):
                return only.upper()
            return "—"

        origin = _iata(ctx.get("origin", "Hong Kong") or "Hong Kong", fallback="HKG")
        dest = _iata(
            ctx.get("arrive_airport") or "",
            fallback=ctx.get("destination", "") or "",
        )
        if dest == "—":
            dest = _iata(ctx.get("destination", "") or "", fallback="MIA")
        ret_from = _iata(ctx.get("depart_airport") or "", fallback=dest)
        if ret_from == "—":
            ret_from = dest

        if self.agent:
            # Live scrape card wins over sparse/empty booking_links
            try:
                plan_card = (
                    getattr(self.agent.browser, "last_plan_flight_card", None) or {}
                )
                live_card = getattr(self.agent.browser, "last_flight_card", None) or {}
                # Prefer frozen plan card (open-jaw merge) when it has times
                prefer = plan_card if plan_card.get("flight_depart") or plan_card.get(
                    "flight_airline"
                ) else (plan_card or live_card)
                if not prefer.get("flight_depart") and live_card.get("flight_depart"):
                    prefer = {**prefer, **{k: v for k, v in live_card.items() if v}}
                for k, v in prefer.items():
                    if not v:
                        continue
                    cur = self.agent.booking_links.get(k) or ""
                    # Prefer non-empty scrape; overwrite placeholders
                    if not cur or cur in {"--:--", "See Trip.com", "n/a"}:
                        self.agent.booking_links[k] = v
                    elif k in {
                        "flight_depart",
                        "flight_arrive",
                        "flight_airline",
                        "flight_return_depart",
                        "flight_return_arrive",
                        "flight_return_airline",
                        "flight_price",
                        "flight_duration",
                        "flight_stops",
                        "flight_return_duration",
                        "flight_return_stops",
                    }:
                        self.agent.booking_links[k] = v
            except Exception:
                pass
            links = self.agent.booking_links
            airline = links.get("flight_airline", "")
            if is_plausible_airline_name(airline):
                offer.airline = airline
            if links.get("flight_depart"):
                offer.depart_time = links["flight_depart"]
            if links.get("flight_arrive"):
                offer.arrive_time = links["flight_arrive"]
            if links.get("flight_from"):
                offer.depart_airport = _iata(links["flight_from"], fallback=origin)
            if links.get("flight_to"):
                offer.arrive_airport = _iata(links["flight_to"], fallback=dest)
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
                offer.return_depart_airport = _iata(
                    links["flight_return_from"], fallback=ret_from
                )
            if links.get("flight_return_to"):
                offer.return_arrive_airport = _iata(
                    links["flight_return_to"], fallback=origin
                )
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
            if links.get("flight"):
                offer.url = links["flight"] or offer.url
            if links.get("flight_return"):
                offer.return_url = links["flight_return"]

            # Direct apply from frozen plan card onto the offer (bypass any stale links)
            plan_direct = (
                getattr(self.agent.browser, "last_plan_flight_card", None) or {}
            )
            if plan_direct.get("flight_depart") and (
                not offer.depart_time or offer.depart_time == "--:--"
            ):
                offer.depart_time = plan_direct["flight_depart"]
            if plan_direct.get("flight_arrive") and (
                not offer.arrive_time or offer.arrive_time == "--:--"
            ):
                offer.arrive_time = plan_direct["flight_arrive"]
            if is_plausible_airline_name(plan_direct.get("flight_airline", "")) and (
                not is_plausible_airline_name(offer.airline)
                or offer.airline in {"", "Trip.com fare", "See Trip.com for airline"}
            ):
                offer.airline = plan_direct["flight_airline"]
                offer.airline_logo = (
                    plan_direct.get("flight_airline_logo")
                    or airline_logo_url(offer.airline)
                )
            if plan_direct.get("flight_price") and offer.price_label in {
                "",
                "See Trip.com",
            }:
                offer.price_label = plan_direct["flight_price"]
            if plan_direct.get("flight_duration") and offer.duration in {"", "—"}:
                if "night" not in plan_direct["flight_duration"].lower():
                    offer.duration = plan_direct["flight_duration"]
            if plan_direct.get("flight_stops"):
                offer.stops = plan_direct["flight_stops"]
            if plan_direct.get("flight_return_depart") and (
                not offer.return_depart_time or offer.return_depart_time == "--:--"
            ):
                offer.return_depart_time = plan_direct["flight_return_depart"]
            if plan_direct.get("flight_return_arrive") and (
                not offer.return_arrive_time or offer.return_arrive_time == "--:--"
            ):
                offer.return_arrive_time = plan_direct["flight_return_arrive"]
            if is_plausible_airline_name(plan_direct.get("flight_return_airline", "")):
                offer.return_airline = plan_direct["flight_return_airline"]
                offer.return_airline_logo = (
                    plan_direct.get("flight_return_airline_logo")
                    or airline_logo_url(offer.return_airline)
                )
            if plan_direct.get("flight") and not offer.url:
                offer.url = plan_direct["flight"]
            if plan_direct.get("flight_return"):
                offer.return_url = plan_direct["flight_return"]
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
                offer.depart_airport = _iata(alt.depart_airport, fallback=origin)
            if alt.arrive_airport and alt.arrive_airport not in {"", "—"}:
                offer.arrive_airport = _iata(alt.arrive_airport, fallback=dest)

        # Open-jaw regional trips: ALWAYS use context airports (scrape often
        # returns a same-city round-trip that must not win over the plan).
        is_open_jaw = bool(ret_from and dest and ret_from != dest and dest != "—")
        if is_open_jaw:
            offer.arrive_airport = dest
            offer.return_depart_airport = ret_from
            offer.return_arrive_airport = origin or "HKG"
            offer.trip_label = "Open-jaw"
            offer.badge = f"Open-jaw · {dest} in / {ret_from} out"
            if self.agent:
                self.agent.booking_links["flight_to"] = dest
                self.agent.booking_links["flight_return_from"] = ret_from
                self.agent.booking_links["flight_return_to"] = origin or "HKG"
            # Guarantee both search-page links for open-jaw
            if not offer.url and ctx.get("depart_date"):
                offer.url = build_flight_search_url(
                    origin=origin or "HKG",
                    destination=dest,
                    depart_date=str(ctx["depart_date"]),
                    trip_type="oneway",
                )
            if not offer.return_url and ctx.get("return_date"):
                offer.return_url = build_flight_search_url(
                    origin=ret_from,
                    destination=origin or "HKG",
                    depart_date=str(ctx["return_date"]),
                    trip_type="oneway",
                )
            if self.agent:
                if offer.url:
                    self.agent.booking_links["flight"] = offer.url
                if offer.return_url:
                    self.agent.booking_links["flight_return"] = offer.return_url
        else:
            if origin and offer.depart_airport in {"", "—", "HKG"}:
                offer.depart_airport = origin
            if dest and dest != "—" and (
                offer.arrive_airport in {"", "—"}
                or len(re.sub(r"[^A-Za-z]", "", offer.arrive_airport)) != 3
            ):
                offer.arrive_airport = dest
            if ret_from and offer.return_depart_airport in {"", "—"}:
                offer.return_depart_airport = ret_from
            if origin and offer.return_arrive_airport in {"", "—"} and (
                offer.return_depart_time or ret_from
            ):
                offer.return_arrive_airport = origin

        # Final sanitize — never leave multi-word / region names on the card
        offer.depart_airport = _iata(offer.depart_airport, fallback=origin)
        offer.arrive_airport = _iata(offer.arrive_airport, fallback=dest)
        if offer.return_depart_airport:
            offer.return_depart_airport = _iata(
                offer.return_depart_airport, fallback=ret_from
            )
        if offer.return_arrive_airport:
            offer.return_arrive_airport = _iata(
                offer.return_arrive_airport, fallback=origin
            )
        # Fill dates from trip context when scraper didn't emit them
        if not offer.depart_date and ctx.get("depart_date"):
            offer.depart_date = str(ctx["depart_date"])
        if not offer.return_date and ctx.get("return_date"):
            offer.return_date = str(ctx["return_date"])
        # Friendly label when airline still unknown but we have a live fare
        if not is_plausible_airline_name(offer.airline) or offer.airline in {
            "",
            "Trip.com fare",
        }:
            # Never put the route into the airline name slot
            offer.airline = "See Trip.com for airline"
        # Final pass: scraped open-jaw card always wins over placeholders
        if self.agent:
            plan = getattr(self.agent.browser, "last_plan_flight_card", None) or {}
            if plan.get("flight_depart"):
                offer.depart_time = plan["flight_depart"]
            if plan.get("flight_arrive"):
                offer.arrive_time = plan["flight_arrive"]
            if is_plausible_airline_name(plan.get("flight_airline", "")):
                offer.airline = plan["flight_airline"]
                offer.airline_logo = (
                    plan.get("flight_airline_logo") or airline_logo_url(offer.airline)
                )
            if plan.get("flight_price"):
                offer.price_label = plan["flight_price"]
            if plan.get("flight_duration") and "night" not in plan["flight_duration"].lower():
                offer.duration = plan["flight_duration"]
            if plan.get("flight_stops"):
                offer.stops = plan["flight_stops"]
            if plan.get("flight_return_depart"):
                offer.return_depart_time = plan["flight_return_depart"]
            if plan.get("flight_return_arrive"):
                offer.return_arrive_time = plan["flight_return_arrive"]
            if is_plausible_airline_name(plan.get("flight_return_airline", "")):
                offer.return_airline = plan["flight_return_airline"]
                offer.return_airline_logo = (
                    plan.get("flight_return_airline_logo")
                    or airline_logo_url(offer.return_airline)
                )
            if plan.get("flight_return_duration") and "night" not in plan[
                "flight_return_duration"
            ].lower():
                offer.return_duration = plan["flight_return_duration"]
            if plan.get("flight_return_stops"):
                offer.return_stops = plan["flight_return_stops"]
            if plan.get("flight"):
                offer.url = plan["flight"]
            if plan.get("flight_return"):
                offer.return_url = plan["flight_return"]
        if offer.badge in {"", "Recommended"} and offer.price_label not in {"", "See Trip.com"}:
            offer.badge = "Live Trip.com fare"
        if is_plausible_airline_name(offer.airline) and not offer.airline_logo:
            offer.airline_logo = airline_logo_url(offer.airline)
        # Open-jaw: always show the return leg block (airports/dates even if times pending)
        if is_open_jaw:
            if not offer.return_depart_time:
                offer.return_depart_time = "--:--"
            if not offer.return_arrive_time:
                offer.return_arrive_time = "--:--"
            if not offer.return_depart_airport:
                offer.return_depart_airport = ret_from
            if not offer.return_arrive_airport:
                offer.return_arrive_airport = origin or "HKG"
            if not offer.return_date and ctx.get("return_date"):
                offer.return_date = str(ctx["return_date"])
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
            from travel_agent.browser_tools import _is_curated_fallback_name

            low = (name or "").lower()
            return (
                not name
                or name in {"Recommended hotel", "Hotel"}
                or low.startswith("recommended hotel")
                or low.startswith("hotels in ")
                or low.startswith("error:")
                or "status code" in low
                or "not found" in low
                or "ollama" in low
                or name.startswith("#")
                or "day-by-day" in low
                or "sample hotel" in low
                or "rates range" in low
                or "deals & reviews" in low
                or _is_curated_fallback_name(name, dest)
                or ("option" in low and "hotel" in low and len(name) > 40)
                or len(name) > 100
            )

        if self.agent:
            from travel_agent.browser_tools import clean_hotel_display_name

            links = self.agent.booking_links
            live_name = (
                getattr(self.agent.browser, "last_hotel_name", "") or ""
            ).strip()
            cand = clean_hotel_display_name(
                live_name or links.get("hotel_name") or "",
                offer.city or offer.location or dest or "",
            )
            if cand and not _bad_name(cand):
                # Reject China rail-station hotels mapped onto Western cities
                low = cand.lower()
                if not any(
                    m in low
                    for m in ("lishui", "high speed railway", "高铁", "火车站")
                ):
                    offer.name = cand
                elif hotel_name and not _bad_name(hotel_name):
                    offer.name = clean_hotel_display_name(
                        hotel_name, offer.city or dest or ""
                    )
            elif hotel_name and not _bad_name(hotel_name):
                offer.name = clean_hotel_display_name(
                    hotel_name, offer.city or dest or ""
                )
            elif offer.name:
                offer.name = clean_hotel_display_name(
                    offer.name, offer.city or dest or ""
                ) or offer.name
            # Force detail booking URL when Playwright found one
            live_detail = (
                getattr(self.agent.browser, "last_hotel_detail_url", "") or ""
            ).strip()
            if live_detail and is_trusted_hotel_detail_url(live_detail):
                offer.url = (
                    canonicalize_hotel_detail_url(
                        live_detail,
                        checkin=offer.checkin or "",
                        checkout=offer.checkout or "",
                        city=offer.city or offer.location or dest or "",
                    )
                    or live_detail
                )
            elif links.get("hotel") and is_trusted_hotel_detail_url(links["hotel"]):
                offer.url = links["hotel"]
            # If the booking URL is a real detail page but the name is still wrong,
            # read the official Trip.com title / cover from the detail HTML.
            detail_for_meta = ""
            if offer.url and is_trusted_hotel_detail_url(offer.url):
                detail_for_meta = offer.url
            elif live_detail and is_trusted_hotel_detail_url(live_detail):
                detail_for_meta = live_detail
            if detail_for_meta and (
                _bad_name(offer.name)
                or not (offer.image_url or "").startswith("http")
                or not (offer.score or "").strip()
            ):
                try:
                    from travel_agent.browser_tools import _http_fetch_hotel_detail_meta

                    meta = _http_fetch_hotel_detail_meta(detail_for_meta)
                except Exception:
                    meta = {}
                if meta.get("name") and not _bad_name(meta["name"]):
                    offer.name = meta["name"]
                if meta.get("image_url") and (
                    not (offer.image_url or "").startswith("http")
                    or "loremflickr" in (offer.image_url or "").lower()
                ):
                    offer.image_url = meta["image_url"]
                if meta.get("score") and not offer.score:
                    offer.score = meta["score"]
                    offer.score_label = meta.get("score_label") or offer.score_label
                if meta.get("reviews") and not offer.reviews:
                    offer.reviews = meta["reviews"]
                if meta.get("location") and not offer.location:
                    offer.location = meta["location"]
                if meta.get("stars") and not offer.stars:
                    try:
                        offer.stars = int(meta["stars"])
                    except ValueError:
                        pass
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

    def _flight_offer_from_live_card(self, card: dict[str, str]) -> FlightOffer:
        """Build a FlightOffer directly from a scraped open-jaw / flight card dict."""
        from travel_agent.itinerary_parse import FlightOffer

        ctx = self._trip_context
        origin = to_flight_code(ctx.get("origin", "Hong Kong") or "Hong Kong").upper() or "HKG"
        dest = (ctx.get("arrive_airport") or "").strip().upper()
        ret_from = (ctx.get("depart_airport") or "").strip().upper()
        airline = card.get("flight_airline", "")
        ret_airline = card.get("flight_return_airline", "")
        offer = FlightOffer(
            airline=airline if is_plausible_airline_name(airline) else "",
            airline_logo=card.get("flight_airline_logo", "")
            or (airline_logo_url(airline) if is_plausible_airline_name(airline) else ""),
            depart_date=card.get("flight_date") or ctx.get("depart_date", ""),
            depart_time=card.get("flight_depart", ""),
            depart_airport=card.get("flight_from") or origin,
            arrive_time=card.get("flight_arrive", ""),
            arrive_airport=card.get("flight_to") or dest,
            duration=card.get("flight_duration", ""),
            stops=card.get("flight_stops") or "Direct",
            price_label=card.get("flight_price", ""),
            return_airline=ret_airline if is_plausible_airline_name(ret_airline) else "",
            return_airline_logo=card.get("flight_return_airline_logo", "")
            or (
                airline_logo_url(ret_airline)
                if is_plausible_airline_name(ret_airline)
                else ""
            ),
            return_date=card.get("flight_return_date") or ctx.get("return_date", ""),
            return_depart_time=card.get("flight_return_depart", ""),
            return_depart_airport=card.get("flight_return_from") or ret_from,
            return_arrive_time=card.get("flight_return_arrive", ""),
            return_arrive_airport=card.get("flight_return_to") or origin,
            return_duration=card.get("flight_return_duration", ""),
            return_stops=card.get("flight_return_stops") or "Direct",
            url=card.get("flight", ""),
            return_url=card.get("flight_return", ""),
        )
        if dest and ret_from and dest != ret_from:
            offer.trip_label = "Open-jaw"
            offer.badge = f"Open-jaw · {dest} in / {ret_from} out"
            offer.arrive_airport = dest
            offer.return_depart_airport = ret_from
            offer.return_arrive_airport = origin
        else:
            offer.trip_label = "Round-trip"
            if offer.price_label:
                offer.badge = "Live Trip.com fare"
        if not is_plausible_airline_name(offer.airline):
            offer.airline = "See Trip.com for airline"
        return offer

    def _apply_plan(self, text: str) -> None:
        raw = (text or "").strip()
        # Never paint Ollama/tool failures into hotel/flight card titles
        if raw.lower().startswith("error:") or "model '" in raw.lower() and "not found" in raw.lower():
            self._set_status(raw[:220], C["danger"])
            self._append_chat("System", raw[:500], "agent")
            # Still show any live scrape cards collected before the failure
            if self.agent:
                live = dict(
                    getattr(self.agent.browser, "last_plan_flight_card", None)
                    or getattr(self.agent.browser, "last_flight_card", None)
                    or {}
                )
                if live.get("flight_depart"):
                    self.flight_row.set_offer(self._flight_offer_from_live_card(live))
            return
        self._last_plan = text
        # Prefer the agent's proposed rough route for airports / multi-stay cards,
        # but never shrink below the nights the user set in the wizard.
        try:
            route = (
                getattr(self.agent.browser, "last_proposed_route", None)
                if self.agent
                else None
            )
            ctx_n = 0
            try:
                ctx_n = max(1, int(self._trip_context.get("nights") or 0))
            except ValueError:
                ctx_n = 0
            if not ctx_n:
                try:
                    d0 = self._trip_context.get("depart_date") or ""
                    d1 = self._trip_context.get("return_date") or ""
                    if d0 and d1:
                        ctx_n = max(
                            1,
                            (
                                date.fromisoformat(d1) - date.fromisoformat(d0)
                            ).days,
                        )
                except ValueError:
                    ctx_n = 0

            if route is not None and getattr(route, "stays", None):
                route_n = sum(max(1, int(getattr(s, "nights", 1) or 1)) for s in route.stays)
                if ctx_n and route_n < ctx_n:
                    from travel_agent.regions import (
                        align_route_to_nights,
                        build_regional_route,
                    )

                    dest = self._trip_context.get("destination") or ""
                    depart = self._trip_context.get("depart_date") or ""
                    rebuilt = build_regional_route(dest, ctx_n, depart_date=depart)
                    if rebuilt is not None:
                        rebuilt.arrive_airport = (
                            route.arrive_airport or rebuilt.arrive_airport
                        )
                        rebuilt.depart_airport = (
                            route.depart_airport or rebuilt.depart_airport
                        )
                        route = rebuilt
                    else:
                        route = align_route_to_nights(
                            route, ctx_n, depart_date=depart
                        )
                    if self.agent:
                        self.agent.browser.last_proposed_route = route
                self._trip_context["arrive_airport"] = (
                    route.arrive_airport or ""
                ).upper()
                self._trip_context["depart_airport"] = (
                    route.depart_airport or ""
                ).upper()
                self._trip_context["region"] = getattr(route, "label", "") or ""
                self._trip_context["stays"] = ";".join(
                    f"{s.city}|{s.nights}|{s.checkin}|{s.checkout}|{s.airport}"
                    for s in route.stays
                )
                self._trip_context["nights"] = str(
                    sum(max(1, int(s.nights or 1)) for s in route.stays)
                )
                self._trip_context["internal_note"] = getattr(
                    route, "internal_note", ""
                ) or ""
            elif ctx_n and not self._trip_context.get("stays"):
                # Single-city: keep a synthetic stay covering the full trip
                dest = self._trip_context.get("destination") or "Destination"
                depart = self._trip_context.get("depart_date") or ""
                ret = self._trip_context.get("return_date") or ""
                self._trip_context["stays"] = (
                    f"{dest}|{ctx_n}|{depart}|{ret}|"
                )
        except Exception:
            pass
        parsed = self._apply_booking_urls(parse_itinerary(text))
        # Apply live flight times BEFORE building day cards (Day 1 must start after landing)
        live = dict(getattr(self, "_live_flight_card", None) or {})
        if not live.get("flight_depart") and self.agent:
            live = dict(
                getattr(self.agent.browser, "last_plan_flight_card", None) or {}
            )
        if not live.get("flight_depart") and self.agent:
            live = dict(getattr(self.agent.browser, "last_flight_card", None) or {})
        if live.get("flight_depart"):
            parsed.flight_offer = self._flight_offer_from_live_card(live)
            if self.agent and live.get("flight_arrive"):
                self.agent.booking_links["flight_arrive"] = live["flight_arrive"]
            if self.agent and live.get("flight_return_depart"):
                self.agent.booking_links["flight_return_depart"] = live[
                    "flight_return_depart"
                ]
        parsed = self._ensure_days(parsed)
        self._render_parsed(parsed)
        if parsed.flight_offer and parsed.flight_offer.depart_time not in {"", "--:--"}:
            self.flight_row.set_offer(parsed.flight_offer)
        err = getattr(self, "_live_flight_error", "") or ""
        if err:
            self._set_status(f"Flight scrape: {err}", C["danger"])
        # If still empty, scrape again in the background and refresh just the flight card
        need_refill = (
            not parsed.flight_offer
            or parsed.flight_offer.depart_time in {"", "--:--"}
            or not is_plausible_airline_name(parsed.flight_offer.airline or "")
        )
        arrive = (self._trip_context.get("arrive_airport") or "").strip().upper()
        ret_from = (self._trip_context.get("depart_airport") or "").strip().upper()
        if need_refill and arrive and ret_from and arrive != ret_from:
            self._queue_open_jaw_refill()
        self._append_chat("System", "Itinerary ready. Refine below if you like.", "agent")

    def _queue_open_jaw_refill(self) -> None:
        """Re-scrape open-jaw legs after the plan paints, then update the flight card."""
        ctx = dict(self._trip_context)

        def job() -> dict[str, str]:
            assert self.agent is not None
            arrive = (ctx.get("arrive_airport") or "").strip().upper()
            ret_from = (ctx.get("depart_airport") or "").strip().upper()
            oj = self.agent.browser.search_open_jaw_flights(
                origin=ctx.get("origin", "Hong Kong") or "Hong Kong",
                arrive_airport=arrive,
                return_airport=ret_from,
                depart_date=ctx.get("depart_date") or "",
                return_date=ctx.get("return_date") or "",
                adults=1,
            )
            self._merge_live_flight(oj)
            self._live_flight_card = dict(oj)
            return oj

        def on_ok(oj: dict[str, str]) -> None:
            if not oj.get("flight_depart"):
                self._set_status(
                    "Still waiting on Trip.com flight times — try Open outbound search.",
                    C["danger"],
                )
                return
            offer = self._flight_offer_from_live_card(oj)
            self.flight_row.set_offer(offer)
            # Rebuild Day 1 / departure day so sightseeing can't start before landing
            if self.agent:
                if oj.get("flight_arrive"):
                    self.agent.booking_links["flight_arrive"] = oj["flight_arrive"]
                if oj.get("flight_return_depart"):
                    self.agent.booking_links["flight_return_depart"] = oj[
                        "flight_return_depart"
                    ]
            if self._last_plan:
                try:
                    parsed = self._apply_booking_urls(parse_itinerary(self._last_plan))
                    if oj.get("flight_depart"):
                        parsed.flight_offer = offer
                    parsed = self._ensure_days(parsed)
                    self._render_parsed(parsed)
                    self.flight_row.set_offer(offer)
                except Exception:
                    pass
            self._set_status("Flight card updated from Trip.com", C["ok"])

        self._set_status(
            "Refreshing flight times from Trip.com…", C["accent_deep"]
        )
        self._run_browser_job(job, on_ok=on_ok, on_err=lambda e: self._set_status(
            f"Flight refill failed: {e}", C["danger"]
        ))

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
        # Prefer live scrape card (most accurate), then booking_links, then offer
        live = dict(getattr(self, "_live_flight_card", None) or {})
        if not live.get("flight_arrive") and self.agent:
            live = dict(
                getattr(self.agent.browser, "last_plan_flight_card", None) or {}
            )
        if live.get("flight_arrive") and live["flight_arrive"] not in {"", "--:--"}:
            arrive_time = live["flight_arrive"].strip()
        if live.get("flight_return_depart") and live["flight_return_depart"] not in {
            "",
            "--:--",
        }:
            return_depart_time = live["flight_return_depart"].strip()
        if self.agent:
            links = self.agent.booking_links
            if not arrive_time:
                arrive_time = (links.get("flight_arrive") or "").strip()
            if not return_depart_time:
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
            attraction_plan=(
                list(getattr(self.agent.browser, "last_attraction_day_plan", None) or [])
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
                from travel_agent.ollama_lifecycle import ensure_ollama_running

                def progress(msg: str) -> None:
                    self.after(
                        0,
                        lambda m=msg: (
                            self._set_status(m, C["muted"]),
                            self._sync_action_buttons(preparing_label=True),
                        ),
                    )

                progress("Preparing… starting Ollama")
                # Start Ollama if needed, then auto-pull the chat model when missing
                resolved = ensure_ollama_running(
                    restart=False,
                    on_progress=progress,
                    model=self.model,
                    ensure_model=True,
                )
                if resolved:
                    self.model = resolved

                progress("Preparing… starting Trip.com browser")
                settings.headless = self.headless
                agent = TravelAgent(
                    model=self.model,
                    on_tool_start=self._on_tool_start,
                    on_tool_end=self._on_tool_end,
                    on_status=lambda msg: self.after(
                        0,
                        lambda m=msg: self._set_status(m, C["muted"]),
                    ),
                )
                agent.start()
                self.agent = agent
                try:
                    self.model = agent.model
                except Exception:
                    pass
                mode = "headless" if self.headless else "browser visible"

                def _ready_ui() -> None:
                    self._ready = True
                    self._sync_action_buttons()
                    self._set_status(
                        f"Ready · {self.model} · Trip.com HK · {mode}", C["ok"]
                    )

                self.after(0, _ready_ui)
            except Exception as exc:
                def _fail_ui(e: Exception = exc) -> None:
                    self._ready = False
                    self._sync_action_buttons(preparing_label=True)
                    self._set_status(f"Startup failed: {e}", C["danger"])
                    messagebox.showerror(
                        "Startup error",
                        f"{e}\n\n"
                        "Need Ollama on PATH. This app starts Ollama and downloads "
                        f"{self.model} automatically when missing.\n\n"
                        "For the browser, run once:\n"
                        "  python -m playwright install chromium\n"
                        "Or install Google Chrome / Microsoft Edge.",
                    )

                self.after(0, _fail_ui)
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
            target=worker_loop, daemon=True, name="travel-agent-browser"
        )
        self._browser_thread.start()

    def _on_tool_start(self, name: str, args: dict) -> None:
        """Update status while Trip.com tools run (browser thread → Tk)."""
        labels = {
            "propose_trip_route": "Proposing rough route…",
            "plan_trip": "Planning trip (flights + hotels + cars on Trip.com)…",
            "search_flights": "Searching flights on Trip.com…",
            "search_hotels": "Searching hotels on Trip.com…",
            "compare_flight_prices": "Comparing flight prices…",
            "compare_hotel_prices": "Comparing hotel prices…",
            "search_trains": "Searching trains…",
            "search_transfers": "Searching transfers…",
            "search_cars": "Searching car rentals on Trip.com…",
            "search_attractions": "Looking up attractions…",
        }
        dest = ""
        if isinstance(args, dict):
            dest = (
                args.get("destination")
                or args.get("hotel_city")
                or args.get("city")
                or args.get("location")
                or ""
            )
        msg = labels.get(name, f"Running {name}…")
        if dest:
            msg = f"{msg} ({dest})"
        self.after(0, lambda m=msg: self._set_status(m, C["accent_deep"]))
        if name in {"search_flights", "compare_flight_prices", "plan_trip"}:
            self.after(
                0,
                lambda: self.flight_row.set_loading("Searching Trip.com flights…"),
            )
        if name in {"search_hotels", "compare_hotel_prices", "plan_trip"}:
            self.after(
                0,
                lambda: self.hotel_row.set_loading("Searching Trip.com hotels…"),
            )
        if name == "search_cars" or (
            name == "plan_trip" and self._trip_context.get("rent_car")
        ):

            def _car_loading() -> None:
                if not self.car_row.winfo_ismapped():
                    self.car_row.pack(fill="x", pady=(4, 8))
                self.car_row.set_loading("Searching Trip.com car hire…")

            self.after(0, _car_loading)

    def _on_tool_end(self, name: str, preview: str) -> None:
        snippet = (preview or "").replace("\n", " ").strip()[:80]
        msg = f"Finished {name}" + (f" · {snippet}…" if snippet else "")
        self.after(0, lambda m=msg: self._set_status(m, C["muted"]))

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.header.set_status(text, color or C["muted"])

    def _sync_action_buttons(self, *, preparing_label: bool = False) -> None:
        """Enable Generate only when boot finished and not busy; gray while preparing."""
        can_click = bool(self._ready and self.agent and not self._busy)
        state = "normal" if can_click else "disabled"
        if getattr(self, "generate_btn", None):
            self.generate_btn.configure(state=state)
            if preparing_label or not self._ready:
                self.generate_btn.configure(text="Preparing…")
            else:
                self.generate_btn.configure(text="Generate itinerary")
        if getattr(self, "regen_btn", None):
            self.regen_btn.configure(state=state)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self._sync_action_buttons()
        if busy:
            self._set_status(label or "Working…", C["accent_deep"])
        elif self._ready and self.agent:
            self._set_status(f"Ready · {self.model} · Trip.com HK", C["ok"])

    def _on_close(self) -> None:
        try:
            self._set_status("Closing…", C["muted"])
        except Exception:
            pass
        try:
            self._browser_jobs.put(None)
            if self._browser_thread and self._browser_thread.is_alive():
                self._browser_thread.join(timeout=5)
        except Exception:
            pass
        # Leave Ollama running so the next launch is faster
        self.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A local LLM AI agent for travel planning (Trip.Planner-style desktop)"
    )
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
