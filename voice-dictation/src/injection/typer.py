"""Putting the transcript into the focused input field.

Primary path: clipboard + Ctrl+V (fast and Unicode-safe).
Fallback: per-character SendInput with KEYEVENTF_UNICODE, including surrogate
pairs, for fields that refuse pasting.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

CLIPBOARD_RESTORE_DELAY = 0.3


if _IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _INPUTunion(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t


class InjectionError(RuntimeError):
    """Raised with a user-facing Russian message."""


# ---------------------------------------------------------------- low level

def _send(inputs: list) -> None:  # pragma: no cover - Windows only
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise InjectionError("Windows отклонила ввод текста")


def _vk_input(vk: int, key_up: bool) -> "INPUT":  # pragma: no cover - Windows only
    flags = KEYEVENTF_KEYUP if key_up else 0
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTunion(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _unicode_input(code_unit: int, key_up: bool) -> "INPUT":  # pragma: no cover
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTunion(
            ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def release_stuck_modifiers() -> None:
    """The hotkey may still be physically held; paste must not inherit it."""
    if not _IS_WINDOWS:
        return
    inputs = []
    for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            inputs.append(_vk_input(vk, key_up=True))
    if inputs:
        _send(inputs)
        time.sleep(0.02)


# ---------------------------------------------------------------- clipboard

def _open_clipboard(attempts: int = 10) -> None:  # pragma: no cover - Windows only
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.03)
    raise InjectionError("Буфер обмена занят другим приложением")


def get_clipboard_text() -> str | None:
    if not _IS_WINDOWS:
        return None
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    if not _IS_WINDOWS:
        return
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise InjectionError("Не хватило памяти для буфера обмена")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        raise InjectionError("Не удалось записать в буфер обмена")
    ctypes.memmove(pointer, ctypes.byref(data), size)
    kernel32.GlobalUnlock(handle)

    _open_clipboard()
    try:
        user32.EmptyClipboard()
        # Ownership of the handle passes to the clipboard on success.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise InjectionError("Не удалось записать в буфер обмена")
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ typing

def paste_via_clipboard(text: str) -> None:
    """Preserves whatever the user had in the clipboard before."""
    if not _IS_WINDOWS:
        raise InjectionError("Вставка текста доступна только в Windows")
    previous = None
    try:
        previous = get_clipboard_text()
    except InjectionError:
        log.debug("Could not read the previous clipboard content", exc_info=True)

    set_clipboard_text(text)
    release_stuck_modifiers()
    _send([
        _vk_input(VK_CONTROL, key_up=False),
        _vk_input(VK_V, key_up=False),
        _vk_input(VK_V, key_up=True),
        _vk_input(VK_CONTROL, key_up=True),
    ])

    if previous is not None:
        time.sleep(CLIPBOARD_RESTORE_DELAY)
        try:
            set_clipboard_text(previous)
        except InjectionError:
            log.debug("Could not restore the previous clipboard content", exc_info=True)


def type_via_sendinput(text: str) -> None:
    """Character by character, surrogate pairs included."""
    if not _IS_WINDOWS:
        raise InjectionError("Вставка текста доступна только в Windows")
    release_stuck_modifiers()
    inputs = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for unit in units:
            inputs.append(_unicode_input(unit, key_up=False))
            inputs.append(_unicode_input(unit, key_up=True))
        # SendInput takes a bounded array; flush in chunks for long texts.
        if len(inputs) >= 200:
            _send(inputs)
            inputs = []
            time.sleep(0.005)
    if inputs:
        _send(inputs)


def insert_text(text: str, mode: str = "clipboard") -> None:
    if mode == "sendinput":
        type_via_sendinput(text)
        return
    try:
        paste_via_clipboard(text)
    except InjectionError:
        log.warning("Clipboard paste failed, falling back to SendInput", exc_info=True)
        type_via_sendinput(text)
