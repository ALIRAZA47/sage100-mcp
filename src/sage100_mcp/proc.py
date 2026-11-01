"""This process's own figures for sage_status and soak runs (SPEC.md §13.5, §15.7): memory, threads, uptime."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

STARTED = time.monotonic()
_WIN32: dict[str, Any] = {}  # GetProcessMemoryInfo and what it needs, made on first use
_WIN32_LOCK = threading.Lock()


def resident_mb() -> float | None:
    """Resident memory in MB: the working set on Windows, RSS elsewhere; None where it can't be read."""
    if sys.platform == "win32":
        try:
            return _working_set_mb()
        except (AttributeError, OSError):  # psapi missing, or sys.platform patched off Windows
            return None
    else:
        try:
            with open("/proc/self/statm", encoding="ascii") as statm:  # Linux: pages
                return round(int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
        except (OSError, ValueError, IndexError):
            pass
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS has no current RSS here: the peak
            return round(peak / (2**20 if sys.platform == "darwin" else 2**10), 1)
        except (ImportError, OSError, ValueError):
            return None


def threads() -> int:
    return threading.active_count()


def uptime_s() -> int:
    return int(time.monotonic() - STARTED)


def _working_set_mb() -> float | None:
    """The working set, from GetProcessMemoryInfo called through declared prototypes.

    Undeclared, ctypes passes GetCurrentProcess's pseudo-handle, -1, as a 32-bit int: a 64-bit process then
    hands the call some other handle, and it fails. The prototypes live on this module's own WinDLL objects,
    so the shared ctypes.windll is left as other code expects it.
    """
    if sys.platform != "win32":
        return None
    import ctypes

    with _WIN32_LOCK:  # two first calls at once would otherwise mix two Counters types
        if not _WIN32:
            _WIN32.update(_prototypes())
    counters = _WIN32["counters"]()
    counters.cb = ctypes.sizeof(counters)
    if not _WIN32["info"](_WIN32["current"](), ctypes.byref(counters), counters.cb):
        return None
    working_set: int = counters.WorkingSetSize
    return round(working_set / 2**20, 1)


def _prototypes() -> dict[str, Any]:
    """PROCESS_MEMORY_COUNTERS, GetCurrentProcess and GetProcessMemoryInfo, each with its C types."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    current = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess
    current.argtypes, current.restype = [], wintypes.HANDLE
    info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    info.restype = wintypes.BOOL
    return {"counters": Counters, "current": current, "info": info}
