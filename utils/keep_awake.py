"""
utils/keep_awake.py

Prevents Windows from sleeping / turning the display off while scraping.
Uses SetThreadExecutionState for the current process only.
"""

from __future__ import annotations

import atexit
import sys
from typing import Optional

# Windows execution-state flags
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_active = False


def _set_state(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return bool(result)
    except Exception:  # noqa: BLE001
        return False


def prevent_sleep(verbose: bool = True) -> bool:
    """
    Keep the system (and display) awake until allow_sleep() is called.

    Returns True when the Windows API call succeeded.
    """
    global _active
    ok = _set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
    if ok:
        _active = True
        atexit.register(allow_sleep)
        if verbose:
            print("[OK] Sleep prevention ON (laptop will stay awake while scraping)")
    elif verbose and sys.platform == "win32":
        print("[WARN] Could not enable sleep prevention")
    return ok


def allow_sleep(verbose: bool = False) -> None:
    """Clear the keep-awake request so Windows power settings apply again."""
    global _active
    if not _active and sys.platform != "win32":
        return
    _set_state(_ES_CONTINUOUS)
    _active = False
    if verbose:
        print("[OK] Sleep prevention OFF")


class KeepAwake:
    """Context manager: prevent sleep inside a `with` block."""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose

    def __enter__(self) -> "KeepAwake":
        prevent_sleep(verbose=self.verbose)
        return self

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        allow_sleep(verbose=self.verbose)
        return None
