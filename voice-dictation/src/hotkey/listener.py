"""Low-level keyboard hook (WH_KEYBOARD_LL) running on its own thread.

The hook must live on a thread that pumps messages, otherwise Windows never
delivers events to it. The callback object is kept alive in an attribute -
if it is garbage collected while the hook is installed the process dies.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional

from .parser import Hotkey, MODIFIER_FAMILY, MODIFIER_VKS

log = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
HC_ACTION = 0

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class HotkeyListener:
    """Press-and-hold detector for one configurable combination.

    ``on_press`` fires once per physical press (auto-repeat is suppressed) and
    ``on_release`` fires when the key or any required modifier goes up. Both
    run on the hook thread - marshal to the GUI thread on the receiving side.
    """

    def __init__(
        self,
        hotkey: Hotkey,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hook = None
        self._proc = None  # live reference to the HOOKPROC, do not drop
        self._held = False
        self._pressed_modifier_vks: set[int] = set()
        self._lock = threading.Lock()
        self._ready = threading.Event()

    # ---------------------------------------------------------------- public
    @property
    def hotkey(self) -> Hotkey:
        return self._hotkey

    def set_hotkey(self, hotkey: Hotkey) -> None:
        """Applied on the fly; a combination held right now is released first."""
        with self._lock:
            if self._held:
                self._held = False
                try:
                    self._on_release()
                except Exception:
                    log.exception("on_release failed while switching hotkey")
            self._hotkey = hotkey
            self._pressed_modifier_vks.clear()

    def start(self) -> None:
        if not _IS_WINDOWS:
            raise RuntimeError("Клавиатурный хук доступен только в Windows")
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="hotkey-hook", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if _IS_WINDOWS and self._thread_id:  # pragma: no cover - Windows only
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=3.0)
        self._thread = None
        self._thread_id = 0

    # --------------------------------------------------------------- private
    def _run(self) -> None:  # pragma: no cover - Windows only
        self._thread_id = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            log.error("SetWindowsHookExW failed: %s", ctypes.get_last_error())
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):  # WM_QUIT or error
                break
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._proc = None

    def _callback(self, n_code, w_param, l_param):  # pragma: no cover - Windows only
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            swallow = self._handle_key(int(info.vkCode), int(w_param))
        except Exception:
            log.exception("Keyboard hook callback failed")
            swallow = False
        if swallow:
            return 1  # do not pass the combination to the focused application
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _handle_key(self, vk: int, message: int) -> bool:
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP)
        if not (is_down or is_up):
            return False

        with self._lock:
            hotkey = self._hotkey
            modifier_vks = hotkey.all_modifier_vks()

            if vk in modifier_vks:
                if is_down:
                    self._pressed_modifier_vks.add(vk)
                else:
                    self._pressed_modifier_vks.discard(vk)
                    # Releasing any required modifier ends the hold.
                    if self._held:
                        self._held = False
                        self._fire(self._on_release)
                return False

            if vk != hotkey.key_vk:
                return False

            if is_down:
                if self._held:
                    return True  # auto-repeat: swallow, but do not re-fire
                if not self._modifiers_satisfied(hotkey):
                    return False
                self._held = True
                self._fire(self._on_press)
                return True

            # key up
            if self._held:
                self._held = False
                self._fire(self._on_release)
                return True
            return False

    def _modifiers_satisfied(self, hotkey: Hotkey) -> bool:
        for name in hotkey.modifiers:
            wanted = MODIFIER_VKS[name]
            if not any(vk in self._pressed_modifier_vks for vk in wanted):
                return False
        # Reject extra modifiers so Ctrl+Alt+Space does not fire on
        # Ctrl+Alt+Shift+Space, which usually belongs to another application.
        wanted_families = {MODIFIER_FAMILY[name] for name in hotkey.modifiers}
        for vk in self._pressed_modifier_vks:
            family = _family_of_vk(vk)
            if family is not None and family not in wanted_families:
                return False
        return True

    @staticmethod
    def _fire(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            log.exception("Hotkey callback failed")


def _family_of_vk(vk: int) -> str | None:
    for name, vks in MODIFIER_VKS.items():
        if vk in vks:
            return MODIFIER_FAMILY[name]
    return None
