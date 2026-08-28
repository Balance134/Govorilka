"""Putting the transcript into the focused input field.

Primary path: clipboard + Ctrl+V (fast and Unicode-safe).
Fallback: per-character SendInput with KEYEVENTF_UNICODE, including surrogate
pairs, for fields that refuse pasting.
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
import time
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_RETURN = 0x0D
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3

# The right-hand modifiers live on the extended part of the keyboard: their
# key-up is only recognised with KEYEVENTF_EXTENDEDKEY.
EXTENDED_KEYS = frozenset({VK_RCONTROL, VK_RMENU, VK_RWIN})

# Side-specific virtual keys, so that a held Right Ctrl or AltGr is released
# too. The generic VK_CONTROL/VK_MENU/VK_SHIFT map to the left key only.
SIDED_MODIFIERS = (VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LSHIFT, VK_RSHIFT)
WIN_KEYS = (VK_LWIN, VK_RWIN)

# How long to wait before putting the user's own clipboard content back.
# The target application pastes asynchronously: restoring too early means it
# pastes the OLD text instead of the transcript, which is worse than making the
# user wait, so the delay is deliberately generous.
CLIPBOARD_RESTORE_DELAY = 1.2

# Kinds of key event produced by text_to_key_events().
EVENT_UNICODE = "unicode"
EVENT_VK = "vk"

_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


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

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTunion(ctypes.Union):
        # The real members, not a padding blob: a blob would make sizeof(INPUT)
        # 36 instead of 28 on 32-bit Python and SendInput would reject it.
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t


class InjectionError(RuntimeError):
    """Raised with a user-facing Russian message."""


# ------------------------------------------------------------ pure helpers

def normalize_newlines(text: str) -> str:
    """Windows edit controls expect CR LF; a bare U+000A is not a line break."""
    return _NEWLINE_RE.sub("\r\n", text)


def text_to_key_events(text: str) -> list[tuple[str, int]]:
    """Expand text into (kind, code) pairs, one key down/up pair each.

    Characters outside the BMP become their two UTF-16 surrogates, because
    KEYEVENTF_UNICODE carries a single 16-bit code unit at a time.
    Line breaks become a real VK_RETURN press rather than a U+000D character:
    web views and rich-text controls act on the key event and ignore a
    synthesised carriage return.
    """
    events: list[tuple[str, int]] = []
    for char in normalize_newlines(text):
        if char == "\r":
            events.append((EVENT_VK, VK_RETURN))
            continue
        if char == "\n":
            continue  # the LF of a normalised CR LF pair
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            events.append((EVENT_UNICODE, 0xD800 + (code >> 10)))
            events.append((EVENT_UNICODE, 0xDC00 + (code & 0x3FF)))
        else:
            events.append((EVENT_UNICODE, code))
    return events


def modifier_release_plan(is_down: Callable[[int], bool]) -> list[tuple[int, bool]]:
    """(vk, key_up) events that let go of every modifier the user still holds."""
    events: list[tuple[int, bool]] = [(vk, True) for vk in SIDED_MODIFIERS if is_down(vk)]
    held_win = [vk for vk in WIN_KEYS if is_down(vk)]
    if held_win:
        # Releasing a lone Win key opens the Start menu, which steals focus and
        # swallows the paste. A harmless Ctrl tap while Win is still down makes
        # the shell treat the press as part of a chord.
        events.append((VK_CONTROL, False))
        events.append((VK_CONTROL, True))
        events.extend((vk, True) for vk in held_win)
    return events


# ---------------------------------------------------------------- low level

def _send(inputs: list) -> None:  # pragma: no cover - Windows only
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        log.warning(
            "SendInput accepted %s of %s events, last error %s",
            sent, len(inputs), ctypes.get_last_error(),
        )
        raise InjectionError("Windows отклонила ввод текста")


def _vk_input(vk: int, key_up: bool) -> "INPUT":  # pragma: no cover - Windows only
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
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


def _is_key_down(vk: int) -> bool:  # pragma: no cover - Windows only
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def release_stuck_modifiers() -> None:
    """The hotkey may still be physically held; paste must not inherit it."""
    if not _IS_WINDOWS:
        return
    plan = modifier_release_plan(_is_key_down)
    if plan:
        _send([_vk_input(vk, key_up=key_up) for vk, key_up in plan])
        time.sleep(0.02)


# ---------------------------------------------------------------- clipboard

_clipboard_window = None


def _clipboard_owner() -> int:  # pragma: no cover - Windows only
    """A message-only window to own the clipboard.

    OpenClipboard(NULL) leaves the clipboard ownerless, and SetClipboardData
    then fails outright, so every clipboard call goes through this window. It
    is created once and lives until the process exits.
    """
    global _clipboard_window
    if _clipboard_window is None:
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "VoiceDictationClipboard", 0, 0, 0, 0, 0,
            wintypes.HWND(HWND_MESSAGE), None, None, None,
        )
        if not hwnd:
            log.error("CreateWindowExW failed, error %s", ctypes.get_last_error())
            raise InjectionError("Не удалось подготовить буфер обмена")
        _clipboard_window = hwnd
    return _clipboard_window


def _open_clipboard(attempts: int = 10) -> None:  # pragma: no cover - Windows only
    owner = _clipboard_owner()
    for _ in range(attempts):
        if user32.OpenClipboard(owner):
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
        count = int(kernel32.GlobalSize(handle)) // ctypes.sizeof(ctypes.c_wchar)
        if not count:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            # Bounded by the real block size in case the data is not terminated.
            return ctypes.wstring_at(pointer, count).split("\0", 1)[0]
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    """Replaces the clipboard with `text`.

    Only CF_UNICODETEXT is handled here and in get_clipboard_text(): an image,
    a file list or rich text sitting on the clipboard is destroyed by
    EmptyClipboard and cannot be restored afterwards. Enumerating and copying
    every format would mean rendering delayed formats too, which is far more
    machinery than a dictation tool needs.
    """
    if not _IS_WINDOWS:
        return
    data = ctypes.create_unicode_buffer(normalize_newlines(text))
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise InjectionError("Не хватило памяти для буфера обмена")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise InjectionError("Не удалось записать в буфер обмена")
    ctypes.memmove(pointer, ctypes.byref(data), size)
    kernel32.GlobalUnlock(handle)

    _open_clipboard()
    owned = False
    try:
        if not user32.EmptyClipboard():
            raise InjectionError(
                f"Не удалось очистить буфер обмена [код {ctypes.get_last_error()}]"
            )
        # Ownership of the handle passes to the clipboard on success.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise InjectionError(
                f"Не удалось записать в буфер обмена [код {ctypes.get_last_error()}]"
            )
        owned = True
    finally:
        user32.CloseClipboard()
        if not owned:
            kernel32.GlobalFree(handle)


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

    pasted = False
    try:
        set_clipboard_text(text)
        release_stuck_modifiers()
        _send([
            _vk_input(VK_CONTROL, key_up=False),
            _vk_input(VK_V, key_up=False),
            _vk_input(VK_V, key_up=True),
            _vk_input(VK_CONTROL, key_up=True),
        ])
        pasted = True
    finally:
        # Even when the paste failed the caller retypes the text, so the user's
        # own clipboard must come back either way.
        if previous is not None:
            if pasted:
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
    events = text_to_key_events(text)
    inputs = []
    delivered = 0
    pending = 0

    def flush() -> None:
        nonlocal inputs, delivered, pending
        try:
            _send(inputs)
        except InjectionError:
            log.warning("Typed only %s of %s key events before failing", delivered, len(events))
            raise
        delivered += pending
        inputs = []
        pending = 0

    for kind, code in events:
        if kind == EVENT_VK:
            inputs.append(_vk_input(code, key_up=False))
            inputs.append(_vk_input(code, key_up=True))
        else:
            inputs.append(_unicode_input(code, key_up=False))
            inputs.append(_unicode_input(code, key_up=True))
        pending += 1
        # SendInput takes a bounded array; flush in chunks for long texts.
        if len(inputs) >= 200:
            flush()
            time.sleep(0.005)
    if inputs:
        flush()


def insert_text(text: str, mode: str = "clipboard") -> None:
    if mode == "sendinput":
        type_via_sendinput(text)
        return
    try:
        paste_via_clipboard(text)
    except InjectionError:
        log.warning("Clipboard paste failed, falling back to SendInput", exc_info=True)
        type_via_sendinput(text)
