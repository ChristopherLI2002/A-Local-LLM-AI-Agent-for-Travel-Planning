"""Start / stop the local Ollama server for the Voyage app (no console flash)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Callable

from travel_agent.config import settings

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0

# Process name prefixes to stop (case-insensitive, without .exe)
_OLLAMA_NAME_PREFIXES = ("ollama",)


def ollama_bin() -> str | None:
    return shutil.which("ollama")


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= _STARTF_USESHOWWINDOW
    si.wShowWindow = _SW_HIDE
    return si


def _run_hidden(args: list[str], *, timeout: float = 20) -> None:
    """Run a subprocess with no visible console window."""
    kwargs: dict = {
        "args": args,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["startupinfo"] = _hidden_startupinfo()
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    subprocess.run(**kwargs)


def is_ollama_ready(host: str | None = None, timeout: float = 2.0) -> bool:
    base = (host or settings.ollama_host).rstrip("/")
    url = f"{base}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _stop_ollama_win32() -> None:
    """Terminate Ollama processes via Win32 API — no taskkill CMD windows."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_TERMINATE = 0x0001
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
    CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    Process32FirstW = kernel32.Process32FirstW
    Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    Process32FirstW.restype = wintypes.BOOL

    Process32NextW = kernel32.Process32NextW
    Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    Process32NextW.restype = wintypes.BOOL

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    TerminateProcess = kernel32.TerminateProcess
    TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    TerminateProcess.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle

    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return

    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = Process32FirstW(snap, ctypes.byref(entry))
        pids: list[int] = []
        while ok:
            name = (entry.szExeFile or "").lower()
            if any(name.startswith(p) for p in _OLLAMA_NAME_PREFIXES):
                # Don't kill ourselves if somehow named oddly
                if entry.th32ProcessID and entry.th32ProcessID != 0:
                    pids.append(int(entry.th32ProcessID))
            ok = Process32NextW(snap, ctypes.byref(entry))

        for pid in pids:
            handle = OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                try:
                    TerminateProcess(handle, 1)
                finally:
                    CloseHandle(handle)
    finally:
        CloseHandle(snap)


def stop_ollama() -> None:
    """Force-quit local Ollama processes without flashing console windows."""
    if sys.platform == "win32":
        try:
            _stop_ollama_win32()
        except Exception:
            # Last resort: hidden taskkill (still no visible window)
            for name in ("ollama.exe", "ollama app.exe", "ollama_llama_server.exe"):
                try:
                    _run_hidden(["taskkill", "/F", "/IM", name, "/T"], timeout=15)
                except Exception:
                    pass
        return

    for pattern in ("ollama serve", "ollama runner", "ollama"):
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass


def start_ollama_serve() -> subprocess.Popen[bytes] | None:
    """Launch `ollama serve` hidden in the background."""
    bin_path = ollama_bin()
    if not bin_path:
        raise FileNotFoundError(
            "Ollama not found on PATH. Install from https://ollama.com and try again."
        )

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["startupinfo"] = _hidden_startupinfo()
        # CREATE_NO_WINDOW alone — DETACHED_PROCESS can make Ollama spawn visible consoles
        kwargs["creationflags"] = _CREATE_NO_WINDOW
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen([bin_path, "serve"], **kwargs)


def wait_ollama_ready(
    host: str | None = None,
    *,
    timeout: float = 90.0,
    poll: float = 0.5,
    on_progress: Callable[[str], None] | None = None,
) -> bool:
    """Poll until the Ollama HTTP API answers, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ollama_ready(host):
            if on_progress:
                on_progress("Ollama is ready")
            return True
        if on_progress:
            remaining = max(0, int(deadline - time.monotonic()))
            on_progress(f"Waiting for Ollama… ({remaining}s)")
        time.sleep(poll)
    return False


def ensure_ollama_running(
    *,
    restart: bool = False,
    host: str | None = None,
    timeout: float = 90.0,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Make sure Ollama is reachable.

    By default, if Ollama is already up we leave it alone (avoids console flashes
    from kill/restart). Pass restart=True to force a silent restart.
    """
    host = host or settings.ollama_host

    if not restart and is_ollama_ready(host):
        if on_progress:
            on_progress("Ollama already running")
        return

    if restart:
        if on_progress:
            on_progress("Restarting Ollama…")
        stop_ollama()
        time.sleep(0.8)
    elif on_progress:
        on_progress("Starting Ollama…")

    if is_ollama_ready(host):
        if on_progress:
            on_progress("Ollama is ready")
        return

    if on_progress:
        on_progress("Starting Ollama…")
    start_ollama_serve()

    if not wait_ollama_ready(host, timeout=timeout, on_progress=on_progress):
        raise RuntimeError(
            f"Ollama did not become ready at {host} within {int(timeout)}s."
        )
