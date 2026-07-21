"""Trip.Planner-style desktop UI for the Voyage travel agent (tkinter)."""

from __future__ import annotations

import argparse
import math
import queue
import re
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from tkinter import messagebox, scrolledtext

from travel_agent.agent import TravelAgent
from travel_agent.config import settings
from travel_agent.itinerary_parse import FlightOffer, HotelOffer, ParsedItinerary, parse_itinerary
from travel_agent.places import to_flight_code, to_hotel_city
from travel_agent.planner_query import TRAVEL_STYLES, build_plan_query
from travel_agent.trip_urls import (
    build_flight_search_url,
    build_hotel_list_url,
    fetch_hotel_detail_link,
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

FONT_BRAND = ("Georgia", 34, "bold")
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
            self, text=label.upper(), bg=C["paper"], fg=C["muted"], font=("Segoe UI", 8), anchor="w"
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

        # Compact stacked layout for the narrow bookings column
        air = tk.Frame(self.card, bg="#FFFFFF")
        air.pack(fill="x", pady=(0, 8))
        self.logo = tk.Canvas(air, width=32, height=32, bg="#FFFFFF", highlightthickness=0)
        self.logo.pack(side="left", padx=(0, 8))
        self.airline_lbl = tk.Label(
            air, text="—", bg="#FFFFFF", fg=C["ink"], font=FONT_UI, anchor="w"
        )
        self.airline_lbl.pack(side="left", fill="x", expand=True)

        times = tk.Frame(self.card, bg="#FFFFFF")
        times.pack(fill="x")
        times.columnconfigure(0, weight=1)
        times.columnconfigure(1, weight=2)
        times.columnconfigure(2, weight=1)

        dep = tk.Frame(times, bg="#FFFFFF")
        dep.grid(row=0, column=0, sticky="w")
        self.dep_time = tk.Label(
            dep, text="--:--", bg="#FFFFFF", fg="#111111", font=("Segoe UI Semibold", 15)
        )
        self.dep_time.pack(anchor="w")
        self.dep_airport = tk.Label(
            dep, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.dep_airport.pack(anchor="w")

        mid = tk.Frame(times, bg="#FFFFFF")
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

        arr = tk.Frame(times, bg="#FFFFFF")
        arr.grid(row=0, column=2, sticky="e")
        self.arr_time = tk.Label(
            arr, text="--:--", bg="#FFFFFF", fg="#111111", font=("Segoe UI Semibold", 15)
        )
        self.arr_time.pack(anchor="e")
        self.arr_airport = tk.Label(
            arr, text="—", bg="#FFFFFF", fg=C["muted"], font=FONT_SMALL
        )
        self.arr_airport.pack(anchor="e")

        bottom = tk.Frame(self.card, bg="#FFFFFF")
        bottom.pack(fill="x", pady=(10, 0))
        price_col = tk.Frame(bottom, bg="#FFFFFF")
        price_col.pack(side="left")
        self.price = tk.Label(
            price_col,
            text="—",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=("Segoe UI Semibold", 14),
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

    def _draw_path(self) -> None:
        self.path.delete("all")
        w = max(self.path.winfo_width(), 40)
        y = 7
        self.path.create_line(8, y, w - 8, y, fill="#C5CDD6", width=2)
        self.path.create_oval(4, y - 3, 10, y + 3, fill="#C5CDD6", outline="")
        self.path.create_oval(w - 10, y - 3, w - 4, y + 3, fill="#C5CDD6", outline="")

    def _draw_logo(self, initials: str) -> None:
        self.logo.delete("all")
        self.logo.create_polygon(16, 2, 30, 28, 2, 28, fill=C["badge_teal"], outline="")
        self.logo.create_text(
            16, 18, text=(initials or "TP")[:3].upper(), fill="#FFFFFF", font=("Segoe UI", 7, "bold")
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
        self.airline_lbl.configure(text="Searching Trip.com…")
        self._draw_logo("…")
        self.dep_time.configure(text="--:--")
        self.arr_time.configure(text="--:--")
        self.dep_airport.configure(text="—")
        self.arr_airport.configure(text="—")
        self.duration.configure(text="—")
        self.stops.configure(text="—")
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
                font=("Segoe UI", 8),
                padx=7,
                pady=2,
            ).pack()

    def set_offer(self, offer: FlightOffer) -> None:
        self._clear_badges()
        self._add_badge(offer.badge or "Recommended", filled=True)
        if offer.baggage:
            self._add_badge(offer.baggage, filled=False)

        initials = "".join(w[0] for w in offer.airline.split()[:3] if w) or "TP"
        self._draw_logo(initials)
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


class HotelRowCard(tk.Frame):
    """Trip.com-style hotel listing card (photo, name, stars, score, room, price)."""

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, bg=C["paper"], **kwargs)
        tk.Label(
            self,
            text="Recommended hotel",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_UI_BOLD,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        shell = tk.Frame(self, bg=C["line"], padx=1, pady=1)
        shell.pack(fill="x")
        self.card = tk.Frame(shell, bg="#FFFFFF")
        self.card.pack(fill="x")

        body = tk.Frame(self.card, bg="#FFFFFF")
        body.pack(fill="x")

        # Compact photo on top for the narrow bookings column
        self.photo = tk.Canvas(
            body, width=260, height=96, bg="#2A3340", highlightthickness=0
        )
        self.photo.pack(fill="x")
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
            font=("Segoe UI Semibold", 13),
            anchor="w",
        )
        self.name_lbl.pack(side="left")
        self.stars_lbl = tk.Label(
            left_h, text="", bg="#FFFFFF", fg="#E6A800", font=("Segoe UI", 11), anchor="w"
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
            score_txt, text="", bg="#FFFFFF", fg=C["muted"], font=("Segoe UI", 8)
        )
        self.reviews_lbl.pack(anchor="e")
        self.score_badge = tk.Label(
            score_box,
            text="—",
            bg="#1B3A6B",
            fg="#FFFFFF",
            font=("Segoe UI Semibold", 12),
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
            room_txt, text="", bg="#FFFFFF", fg="#4A5560", font=("Segoe UI", 8), anchor="w"
        )
        self.social_lbl.pack(anchor="w", pady=(4, 0))

        price_col = tk.Frame(mid, bg="#FFFFFF")
        price_col.grid(row=0, column=1, sticky="ne", padx=(12, 0))
        self.price_lbl = tk.Label(
            price_col,
            text="—",
            bg="#FFFFFF",
            fg=C["trip_blue"],
            font=("Segoe UI Semibold", 16),
        )
        self.price_lbl.pack(anchor="e")
        self.total_lbl = tk.Label(
            price_col,
            text="",
            bg="#FFFFFF",
            fg=C["muted"],
            font=("Segoe UI", 8),
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

    def _draw_photo_placeholder(self, title: str = "Hotel") -> None:
        self.photo.delete("all")
        w, h = 260, 96
        for i in range(10):
            t = i / 9
            color = _lerp_hex("#1A2230", "#C47A3A", t * 0.55)
            self.photo.create_rectangle(
                0, int(h * i / 10), w, int(h * (i + 1) / 10) + 1, outline="", fill=color
            )
        self.photo.create_rectangle(24, 28, 110, 90, fill="#243041", outline="")
        self.photo.create_rectangle(34, 38, 46, 50, fill="#F0C878", outline="")
        self.photo.create_rectangle(56, 38, 68, 50, fill="#F0C878", outline="")
        self.photo.create_rectangle(78, 38, 90, 50, fill="#E8B86A", outline="")
        self.photo.create_rectangle(34, 58, 46, 70, fill="#E8B86A", outline="")
        self.photo.create_rectangle(56, 58, 68, 70, fill="#F0C878", outline="")
        self.photo.create_rectangle(58, 74, 78, 90, fill="#1A2230", outline="")
        self.photo.create_oval(228, 8, 250, 30, fill="#FFFFFF", outline="")
        self.photo.create_text(239, 19, text="♡", fill="#1B3A6B", font=("Segoe UI", 10))
        self.photo.create_text(
            130, 18, text=(title[:18] if title else "Hotel"), fill="#FFFFFF", font=("Segoe UI", 8)
        )

    def _open(self, _e: object | None = None) -> None:
        if self._url and is_trusted_hotel_detail_url(self._url):
            webbrowser.open(self._url)
        elif self._url and "trip.com" in self._url.lower():
            messagebox.showwarning(
                "Invalid link",
                "This hotel link looks invalid. Regenerate the trip to refresh it.",
            )
        elif self._url:
            messagebox.showwarning("Invalid link", "No valid Trip.com booking link is available yet.")
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
        self._draw_photo_placeholder(offer.name)
        self.name_lbl.configure(text=offer.name)
        self.stars_lbl.configure(text="★" * max(0, min(offer.stars, 5)))
        self.score_badge.configure(text=offer.score or "—")
        self.score_word.configure(text=offer.score_label or "")
        self.reviews_lbl.configure(text=offer.reviews or "")
        self.location_lbl.configure(text=f"Loc · {offer.location}")
        self.features_lbl.configure(text=f"Highlights · {offer.features}")
        self.room_lbl.configure(text=offer.room_type)
        self.beds_lbl.configure(text=offer.beds)
        self.social_lbl.configure(text=offer.social_proof)
        self.price_lbl.configure(text=offer.price_label)
        self.total_lbl.configure(text=offer.total_label or "Total (incl. taxes & fees): see Trip.com")
        self._url = offer.url


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

        self.title("Voyage — Trip.Planner-style Travel Agent")
        self.geometry("1080x780")
        self.minsize(880, 600)
        self.configure(bg=C["paper"])
        try:
            self.tk.call("tk", "scaling", 1.12)
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
        self.step_label = tk.Label(
            top_bar,
            text="Step 1 of 3 — Destination",
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
        self.wizard.pack(fill="both", expand=True)

        self.step1 = tk.Frame(self.wizard, bg=C["paper"])
        self.step2 = tk.Frame(self.wizard, bg=C["paper"])
        self.step3 = tk.Frame(self.wizard, bg=C["paper"])

        # Step 1 — Destination
        tk.Label(
            self.step1,
            text="Where next?",
            bg=C["paper"],
            fg=C["ink"],
            font=("Georgia", 26),
            anchor="w",
        ).pack(fill="x", pady=(24, 6))
        tk.Label(
            self.step1,
            text="One destination. Voyage builds flights, hotels, and a day-by-day plan.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
        ).pack(fill="x", pady=(0, 20))

        self.dest_var = tk.StringVar()
        self.origin_var = tk.StringVar(value="Hong Kong")
        Field(self.step1, "Destination (city)", self.dest_var).pack(fill="x", pady=(0, 12))
        Field(self.step1, "Flying from", self.origin_var).pack(fill="x", pady=(0, 24))
        row1 = tk.Frame(self.step1, bg=C["paper"])
        row1.pack(fill="x")
        PillButton(row1, "Continue", command=lambda: self._wizard_next(2), width=140).pack(
            side="left"
        )

        # Step 2 — Duration
        tk.Label(
            self.step2,
            text="How long?",
            bg=C["paper"],
            fg=C["ink"],
            font=("Georgia", 26),
            anchor="w",
        ).pack(fill="x", pady=(24, 6))
        tk.Label(
            self.step2,
            text="Default is tomorrow for 7 nights — same rhythm as Trip.Planner.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
        ).pack(fill="x", pady=(0, 20))

        self.nights_var = tk.StringVar(value="7")
        self.depart_var = tk.StringVar(value=_default_depart())
        self.return_var = tk.StringVar(value=_default_return(7))
        self.budget_var = tk.StringVar(value="12000")
        self.depart_var.trace_add("write", self._sync_return)
        self.nights_var.trace_add("write", self._sync_return)

        grid = tk.Frame(self.step2, bg=C["paper"])
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
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 12)
        )

        row2 = tk.Frame(self.step2, bg=C["paper"])
        row2.pack(fill="x", pady=(16, 0))
        PillButton(row2, "Back", command=lambda: self._wizard_next(1), primary=False, width=110).pack(
            side="left"
        )
        PillButton(row2, "Continue", command=lambda: self._wizard_next(3), width=140).pack(
            side="left", padx=10
        )

        # Step 3 — Travel style
        tk.Label(
            self.step3,
            text="Your travel style",
            bg=C["paper"],
            fg=C["ink"],
            font=("Georgia", 26),
            anchor="w",
        ).pack(fill="x", pady=(24, 6))
        tk.Label(
            self.step3,
            text="Pick one or more — the itinerary pace and places follow your style.",
            bg=C["paper"],
            fg=C["muted"],
            font=FONT_UI,
            anchor="w",
        ).pack(fill="x", pady=(0, 18))

        chips = tk.Frame(self.step3, bg=C["paper"])
        chips.pack(fill="x", pady=(0, 24))
        for i, style in enumerate(TRAVEL_STYLES):
            var = tk.BooleanVar(value=(style == "First-time"))
            self._style_vars[style] = var
            Chip(chips, style, var).grid(row=i // 3, column=i % 3, padx=(0, 10), pady=6, sticky="w")

        row3 = tk.Frame(self.step3, bg=C["paper"])
        row3.pack(fill="x")
        PillButton(row3, "Back", command=lambda: self._wizard_next(2), primary=False, width=110).pack(
            side="left"
        )
        self.generate_btn = PillButton(
            row3, "Generate itinerary", command=self._on_generate, width=190
        )
        self.generate_btn.pack(side="left", padx=10)

    def _sync_return(self, *_args: object) -> None:
        try:
            depart = date.fromisoformat(self.depart_var.get().strip())
            nights = max(1, int(self.nights_var.get().strip() or "7"))
        except ValueError:
            return
        self.return_var.set((depart + timedelta(days=nights)).isoformat())

    def _wizard_next(self, step: int) -> None:
        if step == 2 and not self.dest_var.get().strip():
            messagebox.showerror("Destination", "Enter a destination city.")
            return
        if step == 3:
            try:
                float(self.budget_var.get().strip())
                date.fromisoformat(self.depart_var.get().strip())
                int(self.nights_var.get().strip())
            except ValueError:
                messagebox.showerror("Duration", "Check nights, dates, and budget.")
                return
        self._show_wizard_step(step)

    def _show_wizard_step(self, step: int) -> None:
        self._wizard_step = step
        self.results.pack_forget()
        self.wizard.pack(fill="both", expand=True)
        for fr in (self.step1, self.step2, self.step3):
            fr.pack_forget()
        labels = {
            1: "Step 1 of 3 — Destination",
            2: "Step 2 of 3 — Duration",
            3: "Step 3 of 3 — Travel style",
        }
        self.step_label.configure(text=labels.get(step, ""))
        {1: self.step1, 2: self.step2, 3: self.step3}[step].pack(fill="both", expand=True)
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
        self.hotel_row = HotelRowCard(left)
        self.hotel_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            right,
            text="Day-by-day itinerary",
            bg=C["paper"],
            fg=C["ink"],
            font=FONT_DISPLAY,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        days_shell = tk.Frame(right, bg=C["line"], padx=1, pady=1)
        days_shell.pack(fill="x", anchor="n")
        self.days_inner = tk.Frame(days_shell, bg=C["field"])
        self.days_inner.pack(fill="x")
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
            stub.arrive_airport = to_flight_code(ctx.get("destination", "") or "").upper() or "—"
            self.flight_row.set_offer(stub)

        if parsed.hotel_offer:
            self.hotel_row.set_offer(parsed.hotel_offer)
        elif parsed.hotel:
            offer = parse_hotel_offer(
                parsed.hotel.body,
                fallback_url=parsed.hotel.urls[0] if parsed.hotel.urls else "",
            )
            self.hotel_row.set_offer(offer)
        else:
            stub = parse_hotel_offer(parsed.raw or "")
            dest = to_hotel_city(self._trip_context.get("destination", "") or "")
            if dest and stub.name in {"", "Recommended hotel"}:
                stub.name = f"Hotels in {dest}"
                stub.location = dest
            self.hotel_row.set_offer(stub)

        self._clear_days()
        if not parsed.days:
            tk.Label(
                self.days_inner,
                text=parsed.raw[:2000] or "No day sections found.",
                bg=C["field"],
                fg=C["ink"],
                font=FONT_BODY,
                justify="left",
                anchor="nw",
                wraplength=480,
            ).pack(fill="x", padx=12, pady=12)
            self.after(80, self._on_page_body_configure)
            return

        for day in parsed.days:
            frame = tk.Frame(self.days_inner, bg=C["field"], padx=14, pady=10)
            frame.pack(fill="x", anchor="n")
            tk.Frame(frame, bg=C["line"], height=1).pack(fill="x", pady=(0, 8))
            tk.Label(
                frame, text=day.title, bg=C["field"], fg=C["accent_deep"], font=FONT_UI_BOLD, anchor="w"
            ).pack(fill="x")
            tk.Label(
                frame,
                text=day.body,
                bg=C["field"],
                fg=C["ink"],
                font=FONT_BODY,
                justify="left",
                anchor="nw",
                wraplength=520,
            ).pack(fill="x", pady=(4, 0))
            for url in day.urls[:2]:
                LinkLabel(frame, url, bg=C["field"], wraplength=520).pack(anchor="w", pady=2)

        if parsed.budget:
            frame = tk.Frame(self.days_inner, bg=C["accent_glow"], padx=14, pady=12)
            frame.pack(fill="x", padx=8, pady=12)
            tk.Label(
                frame, text="Budget snapshot", bg=C["accent_glow"], fg=C["ink"], font=FONT_UI_BOLD
            ).pack(anchor="w")
            tk.Label(
                frame,
                text=parsed.budget,
                bg=C["accent_glow"],
                fg=C["ink"],
                font=FONT_BODY,
                justify="left",
                wraplength=520,
            ).pack(anchor="w", pady=(4, 0))

        self.after(80, self._on_page_body_configure)

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
            self._show_wizard_step(1)
            return
        styles = self._selected_styles()
        if not styles:
            messagebox.showerror("Travel style", "Pick at least one travel style.")
            self._show_wizard_step(3)
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
            self._show_wizard_step(2)
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
        if self.agent:
            self.agent.booking_links = {"flight": "", "hotel": "", "hotel_name": ""}

        self._show_results()
        self.flight_row.set_loading("Comparing flights on Trip.com…")
        self.hotel_row.set_loading("Comparing hotels on Trip.com…")
        self._clear_days()
        tk.Label(
            self.days_inner,
            text="Building your itinerary… this can take a few minutes.",
            bg=C["field"],
            fg=C["muted"],
            font=FONT_BODY,
            padx=12,
            pady=16,
        ).pack(anchor="w")
        self._set_busy(True, "Building Trip.Planner-style itinerary…")

        def job() -> str:
            assert self.agent is not None
            answer = self.agent.chat(query)
            ctx = self._trip_context
            checkout = ctx.get("return_date") or ""
            if not checkout:
                try:
                    checkout = (
                        date.fromisoformat(ctx["depart_date"]) + timedelta(days=7)
                    ).isoformat()
                except ValueError:
                    checkout = ctx["depart_date"]
            if not is_trusted_hotel_detail_url(
                self.agent.booking_links.get("hotel", "")
            ):
                if ctx.get("destination") and ctx.get("depart_date"):
                    live = fetch_hotel_detail_link(
                        self.agent.browser,
                        city=ctx["destination"],
                        checkin=ctx["depart_date"],
                        checkout=checkout,
                    )
                    self._merge_live_hotel(live)
            return answer

        self._run_browser_job(
            job,
            on_ok=lambda answer: self._apply_plan(answer),
            on_err=lambda exc: self._apply_plan(f"Error: {exc}"),
            done=lambda: self._set_busy(False),
        )

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
        ):
            if live.get(key):
                self.agent.booking_links[key] = live[key]

    def _built_booking_urls(self) -> tuple[str, str]:
        ctx = self._trip_context
        if not ctx.get("destination") or not ctx.get("depart_date"):
            return "", ""
        ret = ctx.get("return_date") or None
        flight = build_flight_search_url(
            origin=ctx.get("origin", "Hong Kong"),
            destination=ctx["destination"],
            depart_date=ctx["depart_date"],
            return_date=ret,
        )
        checkout = ret
        if not checkout:
            try:
                checkout = (
                    date.fromisoformat(ctx["depart_date"]) + timedelta(days=7)
                ).isoformat()
            except ValueError:
                checkout = ctx["depart_date"]
        hotel = build_hotel_list_url(
            city=ctx["destination"],
            checkin=ctx["depart_date"],
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
        built_flight, _built_hotel = self._built_booking_urls()

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
            built_url="",
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

        self._enrich_flight_offer(parsed)
        self._enrich_hotel_offer(parsed, hotel_name=hotel_name)
        return parsed

    def _enrich_flight_offer(self, parsed: ParsedItinerary) -> None:
        """Prefer tool-scraped structured flight fields over LLM prose."""
        from travel_agent.itinerary_parse import parse_flight_offer

        offer = parsed.flight_offer
        if not offer:
            return
        ctx = self._trip_context
        origin = to_flight_code(ctx.get("origin", "Hong Kong") or "Hong Kong").upper()
        dest = to_flight_code(ctx.get("destination", "") or "").upper()

        if self.agent:
            links = self.agent.booking_links
            if links.get("flight_airline"):
                offer.airline = links["flight_airline"]
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
            elif offer.price_label in {"", "See Trip.com"} and links.get("flight_price"):
                offer.price_label = links["flight_price"]
            if links.get("flight_option") and (
                offer.airline == "Trip.com fare" or offer.depart_time == "--:--"
            ):
                opt = parse_flight_offer(links["flight_option"], fallback_url=offer.url)
                if offer.airline == "Trip.com fare" and opt.airline != "Trip.com fare":
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
            or offer.airline == "Trip.com fare"
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
            if offer.airline == "Trip.com fare" and alt.airline != "Trip.com fare":
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

        if origin and offer.depart_airport in {"", "—", "HKG"}:
            offer.depart_airport = origin
        if dest and offer.arrive_airport in {"", "—"}:
            offer.arrive_airport = dest
        if offer.badge in {"", "Recommended"} and offer.price_label not in {"", "See Trip.com"}:
            offer.badge = "Live Trip.com fare"

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
            if links.get("hotel_name") and not _bad_name(links["hotel_name"]):
                offer.name = links["hotel_name"]
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
        self._render_parsed(parsed)
        self._append_chat("System", "Itinerary ready. Refine below if you like.", "agent")

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
            "Only use https://hk.trip.com/... URLs from tools."
        )
        self._set_busy(True, "Refining itinerary…")

        def job() -> str:
            assert self.agent is not None
            answer = self.agent.chat(refine)
            if not is_trusted_hotel_detail_url(
                self.agent.booking_links.get("hotel", "")
            ):
                ctx = self._trip_context
                checkout = ctx.get("return_date") or ""
                if not checkout and ctx.get("depart_date"):
                    try:
                        checkout = (
                            date.fromisoformat(ctx["depart_date"]) + timedelta(days=7)
                        ).isoformat()
                    except ValueError:
                        checkout = ctx["depart_date"]
                if ctx.get("destination") and ctx.get("depart_date"):
                    live = fetch_hotel_detail_link(
                        self.agent.browser,
                        city=ctx["destination"],
                        checkin=ctx["depart_date"],
                        checkout=checkout,
                    )
                    self._merge_live_hotel(live)
            return answer

        self._run_browser_job(
            job,
            on_ok=lambda answer: self._apply_refine(answer),
            on_err=lambda exc: self._append_chat("Error", str(exc), "agent"),
            done=lambda: self._set_busy(False),
        )

    def _apply_refine(self, text: str) -> None:
        self._last_plan = text
        self._append_chat("Agent", text[:800] + ("…" if len(text) > 800 else ""), "agent")
        parsed = self._apply_booking_urls(parse_itinerary(text))
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
