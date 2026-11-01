"""This process's own figures for sage_status and soak runs (SPEC.md §13.5, §15.7): memory, threads, uptime."""

from __future__ import annotations

import os
import sys
import threading
import time

STARTED = time.monotonic()


def resident_mb() -> float | None:
    """Resident memory in MB: the working set on Windows, RSS elsewhere; None where it can't be read."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [  # PROCESS_MEMORY_COUNTERS
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

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        try:
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return None
        except (AttributeError, OSError):  # psapi missing, or sys.platform patched off Windows
            return None
        working_set: int = counters.WorkingSetSize
        return round(working_set / 2**20, 1)
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
