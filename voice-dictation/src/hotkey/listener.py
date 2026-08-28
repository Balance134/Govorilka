"""Low-level keyboard hook (WH_KEYBOARD_LL) running on its own thread.

The hook must live on a thread that pumps messages, otherwise Windows never
delivers events to it. The callback object is kept alive in an attribute -
if it is garbage collected while the hook is installed the process dies.

The decision logic (``_handle_key`` and below) is deliberately free of any
WinAPI call so it can be tested on any OS with plain integers.
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
LLKHF_INJECTED = 0x10

# How long start() waits for the hook thread to report back.
HOOK_READY_TIMEOUT_SEC = 5.0

# Every virtual key code that belongs to some modifier family, regardless of
# the hotkey in effect - foreign modifiers must be tracked too.
ALL_MODIFIER_VKS: frozenset[int] = frozenset(
    vk for vks in MODIFIER_VKS.values() for vk in vks
)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = (
        ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
    )
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
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
        self._down_keys: set[int] = set()  # physically held non-modifier keys
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._cancelled = threading.Event()
        self._active = False
        self._install_error: str | None = None

    # ---------------------------------------------------------------- public
    @property
    def hotkey(self) -> Hotkey:
        return self._hotkey

    @property
    def is_running(self) -> bool:
        """True only while a hook of ours is known to be installed."""
        return self._active

    def set_hotkey(self, hotkey: Hotkey) -> None:
        """Applied on the fly; a combination held right now is released first."""
        with self._lock:
            was_held = self._held
            self._held = False
            self._hotkey = hotkey
        # Physically pressed keys stay in _down_keys on purpose: the new
        # combination must not arm on the auto-repeat of a key already down.
        if was_held:
            self._fire(self._on_release)

    def start(self) -> None:
        if not _IS_WINDOWS:
            raise RuntimeError("Клавиатурный хук доступен только в Windows")
        if self._thread is not None:
            if self._active:
                return
            # A thread that outlived its start() or stop() may still install
            # or hold a hook; a second one would double every keypress.
            raise RuntimeError(
                "Прошлый клавиатурный хук ещё не освободился. "
                "Перезапустите программу"
            )
        self._ready.clear()
        self._cancelled.clear()
        self._install_error = None
        thread = threading.Thread(target=self._run, name="hotkey-hook", daemon=True)
        self._thread = thread
        thread.start()
        if not self._ready.wait(timeout=HOOK_READY_TIMEOUT_SEC):
            # The thread is still alive and may install the hook a moment from
            # now. Ask it to unhook itself and keep the reference, so a later
            # start() refuses instead of adding a second hook.
            self._cancelled.set()
            raise RuntimeError(
                "Клавиатурный хук не запустился за 5 секунд. "
                "Перезапустите программу"
            )
        if self._install_error is not None:
            thread.join(timeout=1.0)
            if thread.is_alive():
                self._cancelled.set()
            else:
                self._thread = None
                self._thread_id = 0
            raise RuntimeError(self._install_error)
        self._active = True

    def stop(self) -> None:
        thread = self._thread
        self._active = False
        self._cancelled.set()
        if thread is None:
            return
        if _IS_WINDOWS and self._thread_id:  # pragma: no cover - Windows only
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=3.0)
        if thread.is_alive():
            # Keep the reference so a later start() refuses instead of
            # installing a second hook alongside this one.
            log.error("Hotkey hook thread did not stop within 3 s")
            return
        self._thread = None
        self._thread_id = 0

    # --------------------------------------------------------------- private
    def _run(self) -> None:  # pragma: no cover - Windows only
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            self._proc = HOOKPROC(self._callback)
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                code = ctypes.get_last_error()
                log.error("SetWindowsHookExW failed: %s", code)
                self._install_error = (
                    f"Не удалось перехватить клавиатуру (ошибка Windows {code}). "
                    "Попробуйте запустить программу от имени администратора"
                )
        except Exception:
            log.exception("Keyboard hook installation failed")
            self._install_error = "Не удалось перехватить клавиатуру"
        finally:
            self._ready.set()
        if self._install_error is not None:
            return
        if self._cancelled.is_set():
            # start() gave up waiting: unhook right away instead of leaving an
            # orphan hook behind that keeps firing callbacks.
            log.warning("Hook installed after start() gave up, removing it")
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._proc = None
            return

        try:
            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT or error
                    break
        except Exception:
            log.exception("Keyboard hook message loop failed")
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._proc = None

    def _callback(self, n_code, w_param, l_param):  # pragma: no cover - Windows only
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            swallow = self._handle_key(
                int(info.vkCode), int(w_param), int(info.flags)
            )
        except Exception:
            log.exception("Keyboard hook callback failed")
            swallow = False
        if swallow:
            return 1  # do not pass the combination to the focused application
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _handle_key(self, vk: int, message: int, flags: int = 0) -> bool:
        """Decide under the lock, fire the callback outside it.

        A low-level hook that spends longer than LowLevelHooksTimeout inside
        the callback is silently removed by Windows, so nothing slow may run
        while the lock is held.
        """
        if flags & LLKHF_INJECTED:
            # Our own SendInput (paste, modifier releases) and macro tools come
            # back through this hook; they must not move the state machine.
            return False

        swallow, callback = self._decide(vk, message)
        if callback is not None:
            self._fire(callback)
        return swallow

    def _decide(
        self, vk: int, message: int
    ) -> tuple[bool, Optional[Callable[[], None]]]:
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP)
        if not (is_down or is_up):
            return False, None

        with self._lock:
            hotkey = self._hotkey

            if vk in ALL_MODIFIER_VKS:
                if is_down:
                    self._pressed_modifier_vks.add(vk)
                    return False, None
                self._pressed_modifier_vks.discard(vk)
                # Releasing a required modifier ends the hold; a foreign one
                # (Shift under Ctrl+Alt+Space) leaves the hold alone.
                if self._held and vk in hotkey.all_modifier_vks():
                    self._held = False
                    return False, self._on_release
                return False, None

            if is_down:
                repeat = vk in self._down_keys
                self._down_keys.add(vk)
            else:
                repeat = False
                self._down_keys.discard(vk)

            if vk != hotkey.key_vk:
                return False, None

            if is_down:
                if self._held:
                    return True, None  # auto-repeat: swallow, but do not re-fire
                if repeat:
                    # The key was already down before this combination was
                    # armed - wait for a real press instead of a phantom one.
                    return False, None
                if not self._modifiers_satisfied(hotkey):
                    return False, None
                self._held = True
                return True, self._on_press

            # key up
            if self._held:
                self._held = False
                return True, self._on_release
            return False, None

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
