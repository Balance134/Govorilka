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

# Local\ and not Global\: the global namespace needs SeCreateGlobalPrivilege,
# which a standard user does not have, and it would also stop a second user
# from running their own copy under fast user switching or RDP.
MUTEX_NAME = "Local\\VoiceDictationSingleInstanceMutex"
MESSAGE_NAME = "VoiceDictationShowSettings"
ERROR_ALREADY_EXISTS = 183
HWND_BROADCAST = 0xFFFF
MB_ICONINFORMATION = 0x00000040

if _IS_WINDOWS:  # pragma: no cover - Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.MessageBoxW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT
    ]
    user32.MessageBoxW.restype = ctypes.c_int


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
    posted = False
    if message_id:
        posted = bool(user32.PostMessageW(HWND_BROADCAST, message_id, 0, 0))
    if not posted:
        # The broadcast can be lost: a message-only window never receives it,
        # UIPI drops it across integrity levels, and the first copy may not
        # have created its window yet. Without this the second copy would just
        # vanish with no feedback at all.
        log.warning("Broadcast to the running copy failed: %s", ctypes.get_last_error())
        user32.MessageBoxW(None, "Говорилка уже запущена", "Говорилка", MB_ICONINFORMATION)
