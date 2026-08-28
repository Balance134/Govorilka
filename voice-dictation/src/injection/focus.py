"""Remembering and restoring the window that should receive the text.

Windows refuses SetForegroundWindow from a background process unless the
calling thread is attached to the foreground thread's input queue, hence the
AttachThreadInput dance below.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

SW_RESTORE = 9

if _IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD


def get_foreground_window() -> int:
    if not _IS_WINDOWS:
        return 0
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    if not _IS_WINDOWS or not hwnd:
        return False
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


def belongs_to_this_process(hwnd: int) -> bool:
    """True for our own settings window and tray, which must not be typed into."""
    if not _IS_WINDOWS or not hwnd:
        return False
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value == kernel32.GetCurrentProcessId()


def restore_foreground(hwnd: int) -> bool:
    """Bring the remembered window back to the front. False if impossible."""
    if not _IS_WINDOWS or not hwnd or not is_window(hwnd):
        return False
    if get_foreground_window() == hwnd:
        return True

    target_thread = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = False
    if target_thread and target_thread != current_thread:
        attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        if user32.IsIconic(wintypes.HWND(hwnd)):
            user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)

    return get_foreground_window() == hwnd
