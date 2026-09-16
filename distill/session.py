"""Shared 1-hour budget and resume harness for long distill phases.

Each finished unit is appended to a JSONL file with fsync. --resume skips
completed keys. --max-hours stops before starting a unit that cannot finish.
SIGINT finishes the current unit then exits cleanly.

Optional status_path is overwritten after each successful unit so the newest
checkpoint pointer is always one file to open.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionReport:
    phase: str
    units_done_this_session: int = 0
    units_skipped: int = 0
    units_failed: int = 0
    total_completed: int = 0
    total_planned: int = 0
    avg_unit_sec: float = 0.0
    elapsed_sec: float = 0.0
    stopped_reason: str = ""
    resume_cmd: str = ""
    max_hours: float = 1.0

    def remaining_hours(self) -> float | None:
        left = self.total_planned - self.total_completed
        if left <= 0 or self.avg_unit_sec <= 0:
            return 0.0 if left <= 0 else None
        return (left * self.avg_unit_sec) / 3600.0

    def sessions_left(self, hours_per_session: float | None = None) -> float | None:
        rem = self.remaining_hours()
        if rem is None:
            return None
        per = hours_per_session if hours_per_session is not None else max(0.1, self.max_hours)
        return rem / max(0.1, per)

    def print_report(self) -> None:
        rem = self.remaining_hours()
        sess = self.sessions_left()
        hours_label = f"{self.max_hours:g}h"
        print()
        print("=" * 60)
        print(f"Session report — {self.phase}")
        print("=" * 60)
        print(f"  Done this session : {self.units_done_this_session}")
        print(f"  Skipped (resume)  : {self.units_skipped}")
        print(f"  Failed            : {self.units_failed}")
        print(f"  Total completed   : {self.total_completed} / {self.total_planned}")
        print(f"  Avg unit time     : {self.avg_unit_sec:.1f}s")
        print(f"  Session elapsed   : {self.elapsed_sec / 60:.1f} min")
        if rem is not None:
            print(f"  Est. remaining    : {rem:.1f} h ({sess:.1f} x {hours_label} sessions)")
        if self.stopped_reason:
            print(f"  Stopped because   : {self.stopped_reason}")
        if self.resume_cmd:
            print(f"  Resume with       : {self.resume_cmd}")
        print("=" * 60)


@dataclass
class SessionRunner:
    """Run units under a time budget with append-only JSONL progress."""

    phase: str
    progress_path: Path
    resume_cmd: str
    max_hours: float = 1.0
    key_field: str = "key"
    finish_current_on_sigint: bool = True
    status_path: Path | None = None

    _done_keys: set[str] = field(default_factory=set, init=False)
    _unit_times: list[float] = field(default_factory=list, init=False)
    _stop_requested: bool = field(default=False, init=False)
    _prev_sigint: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        if self.status_path is not None:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self._done_keys = self._load_done_keys()

    def _load_done_keys(self) -> set[str]:
        done: set[str] = set()
        if not self.progress_path.exists():
            return done
        for line in self.progress_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get(self.key_field)
            if key is not None and row.get("ok", True) is not False:
                # Count successful units; failures may be retried unless marked permanent
                if row.get("permanent_fail"):
                    done.add(str(key))
                elif row.get("ok", True):
                    done.add(str(key))
            elif key is not None and row.get("ok") is False and row.get("permanent_fail"):
                done.add(str(key))
        return done

    @property
    def done_keys(self) -> set[str]:
        return set(self._done_keys)

    def is_done(self, key: str) -> bool:
        return str(key) in self._done_keys

    def write_status(self, row: dict[str, Any] | None = None) -> None:
        """Overwrite the latest-checkpoint pointer (status only; JSONL is source of truth)."""
        if self.status_path is None:
            return
        payload = {
            "phase": self.phase,
            "progress_path": str(self.progress_path.resolve()),
            "status_path": str(self.status_path.resolve()),
            "total_completed": len(self._done_keys),
            "last_key": (row or {}).get(self.key_field),
            "last_ok": (row or {}).get("ok"),
            "last_finished_at": (row or {}).get("finished_at"),
            "updated_at": _utc_now(),
            "resume_cmd": self.resume_cmd,
            "max_hours": self.max_hours,
        }
        # Keep a few useful extras when present
        for extra in ("destination", "error", "elapsed_sec", "n_tool_rounds"):
            if row and extra in row:
                payload[extra] = row[extra]
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def append_unit(self, row: dict[str, Any]) -> None:
        """Append one finished unit and fsync; refresh status pointer on success."""
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.progress_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                import os

                os.fsync(f.fileno())
            except OSError:
                # Windows often raises Errno 22 on text-mode fsync; flush is enough.
                pass
        key = row.get(self.key_field)
        if key is not None:
            if row.get("ok", True) or row.get("permanent_fail"):
                self._done_keys.add(str(key))
        if row.get("ok", True):
            self.write_status(row)

    def _install_sigint(self) -> None:
        def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
            if self._stop_requested:
                print("\nSecond interrupt — exiting immediately.", flush=True)
                sys.exit(130)
            self._stop_requested = True
            print(
                "\nSIGINT received — will stop after the current unit finishes.",
                flush=True,
            )

        self._prev_sigint = signal.signal(signal.SIGINT, _handler)

    def _restore_sigint(self) -> None:
        if self._prev_sigint is not None:
            signal.signal(signal.SIGINT, self._prev_sigint)

    def avg_unit_sec(self) -> float:
        if not self._unit_times:
            return 0.0
        return sum(self._unit_times) / len(self._unit_times)

    def should_start_unit(self, *, session_start: float, planned_unit_sec: float | None = None) -> tuple[bool, str]:
        if self._stop_requested:
            return False, "SIGINT"
        budget = max(0.0, self.max_hours) * 3600.0
        elapsed = time.monotonic() - session_start
        remaining = budget - elapsed
        if remaining <= 0:
            return False, "time_budget_exhausted"
        estimate = planned_unit_sec if planned_unit_sec and planned_unit_sec > 0 else self.avg_unit_sec()
        # Leave a small cushion so we don't start a unit we cannot finish
        if estimate > 0 and remaining < estimate * 0.85:
            return False, "insufficient_time_for_next_unit"
        return True, ""

    def run(
        self,
        units: Iterable[tuple[str, Any]],
        work_fn: Callable[[str, Any], dict[str, Any]],
        *,
        total_planned: int | None = None,
        default_unit_sec: float = 480.0,
    ) -> SessionReport:
        """Iterate units; skip done keys; call work_fn(key, payload) → row dict.

        ``work_fn`` must return a dict including ``key`` (or key_field) and preferably ``ok``.
        """
        unit_list = list(units)
        planned = total_planned if total_planned is not None else len(unit_list)
        report = SessionReport(
            phase=self.phase,
            total_planned=planned,
            total_completed=len(self._done_keys),
            resume_cmd=self.resume_cmd,
            max_hours=self.max_hours,
        )
        session_start = time.monotonic()
        self._install_sigint()
        try:
            for key, payload in unit_list:
                key_s = str(key)
                if self.is_done(key_s):
                    report.units_skipped += 1
                    continue
                ok_to_start, reason = self.should_start_unit(
                    session_start=session_start,
                    planned_unit_sec=self.avg_unit_sec() or default_unit_sec,
                )
                if not ok_to_start:
                    report.stopped_reason = reason
                    break

                t0 = time.monotonic()
                try:
                    row = work_fn(key_s, payload)
                except Exception as exc:
                    row = {
                        self.key_field: key_s,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_sec": round(time.monotonic() - t0, 2),
                    }
                if self.key_field not in row:
                    row[self.key_field] = key_s
                elapsed = float(row.get("elapsed_sec") or (time.monotonic() - t0))
                row.setdefault("elapsed_sec", round(elapsed, 2))
                row.setdefault("finished_at", _utc_now())
                self.append_unit(row)
                self._unit_times.append(elapsed)
                if row.get("ok", True):
                    report.units_done_this_session += 1
                else:
                    report.units_failed += 1
                report.total_completed = len(self._done_keys)

                if self._stop_requested and not self.finish_current_on_sigint:
                    report.stopped_reason = "SIGINT"
                    break
                if self._stop_requested:
                    report.stopped_reason = "SIGINT"
                    break
            else:
                if not report.stopped_reason:
                    report.stopped_reason = "all_units_done"
        finally:
            self._restore_sigint()

        report.avg_unit_sec = self.avg_unit_sec()
        report.elapsed_sec = time.monotonic() - session_start
        report.total_completed = len(self._done_keys)
        report.print_report()
        return report


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        try:
            import os

            os.fsync(f.fileno())
        except OSError:
            pass
