"""One running copy only, using a named mutex and a broadcast message.

The second copy asks the first one to show its settings window and exits.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

MUTEX_NAME = "Global\\VoiceDictationSingleInstanceMutex"
MESSAGE_NAME = "VoiceDictationShowSettings"
ERROR_ALREADY_EXISTS = 183
HWND_BROADCAST = 0xFFFF

if _IS_WINDOWS:  # pragma: no cover - Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostMessageW.restype = wintypes.BOOL


class SingleInstance:
    def __init__(self) -> None:
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        """True when this process is the first copy."""
        if not _IS_WINDOWS:
            return True
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if not self._handle:
            log.error("CreateMutexW failed: %s", last_error)
            return True  # do not block startup because of a mutex problem
        self.already_running = last_error == ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self) -> None:
        if _IS_WINDOWS and self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None


def show_settings_message_id() -> int:
    if not _IS_WINDOWS:
        return 0
    return int(user32.RegisterWindowMessageW(MESSAGE_NAME))


def signal_existing_instance() -> None:
    """Ask the already running copy to open its settings window."""
    if not _IS_WINDOWS:
        return
    message_id = show_settings_message_id()
    if message_id:
        user32.PostMessageW(HWND_BROADCAST, message_id, 0, 0)
