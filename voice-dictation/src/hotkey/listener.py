"""Low-level keyboard hook (WH_KEYBOARD_LL) running on its own thread.

The hook must live on a thread that pumps messages, otherwise Windows never
delivers events to it. The callback object is kept alive in an attribute -
if it is garbage collected while the hook is installed the process dies.

Windows drops the hook behind our back - on resume from sleep or
hibernation, after the lock screen, and whenever a callback outstays
LowLevelHooksTimeout. Nothing tells the process about it: the thread stays
alive and simply never hears from the keyboard again. So the hook thread also
owns a message-only window that listens for the resume and session-change
notifications, plus a periodic backstop timer, and re-installs the hook on any
of them.

The decision logic (``_handle_key``, ``reinstall_reason`` and below) is
deliberately free of any WinAPI call so it can be tested on any OS with plain
integers.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from .parser import Hotkey, MODIFIER_FAMILY, MODIFIER_VKS

log = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_SESSION_LOGON = 0x5
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
DEVICE_NOTIFY_WINDOW_HANDLE = 0
HWND_MESSAGE = -3
HC_ACTION = 0
LLKHF_INJECTED = 0x10

# How long start() waits for the hook thread to report back.
HOOK_READY_TIMEOUT_SEC = 5.0

# Backstop: how often the hook thread looks at its own health. Cheap - one
# timer message in a loop that is idle the rest of the time.
HEALTH_TIMER_MS = 30_000
HEALTH_TIMER_ID = 1

# The hook is only suspect when Windows itself saw input this much newer than
# anything the hook was handed. A quiet keyboard proves nothing, so the
# comparison is always against real input, never against the clock alone.
MISSED_INPUT_TOLERANCE_SEC = 60.0

# A hook this fresh is never blamed: the timestamps around an install have not
# settled yet, and a re-install storm would be worse than a late detection.
INSTALL_GRACE_SEC = 10.0

# Every virtual key code that belongs to some modifier family, regardless of
# the hotkey in effect - foreign modifiers must be tracked too.
ALL_MODIFIER_VKS: frozenset[int] = frozenset(
    vk for vks in MODIFIER_VKS.values() for vk in vks
)

_IS_WINDOWS = sys.platform == "win32"

# Window classes cannot be re-registered under a name that a freed WNDPROC
# still owns, so every watchdog window gets a name of its own.
_watch_class_seq = itertools.count(1)

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
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetTickCount.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.SetTimer.argtypes = [
        wintypes.HWND, ULONG_PTR, wintypes.UINT, wintypes.LPVOID
    ]
    user32.SetTimer.restype = ULONG_PTR
    user32.KillTimer.argtypes = [wintypes.HWND, ULONG_PTR]
    user32.KillTimer.restype = wintypes.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    if hasattr(user32, "UnregisterSuspendResumeNotification"):
        user32.UnregisterSuspendResumeNotification.argtypes = [wintypes.HANDLE]
        user32.UnregisterSuspendResumeNotification.restype = wintypes.BOOL


@dataclass(frozen=True)
class HookHealth:
    """Everything the re-install decision is allowed to look at.

    Times are on one monotonic scale, in seconds. ``last_input_at`` is when
    Windows itself last saw any input (GetLastInputInfo), which is the only
    evidence available that is independent of our hook; ``None`` when it could
    not be read.
    """

    now: float
    installed_at: float
    last_hook_event_at: float | None
    last_input_at: float | None
    wake_pending: bool
    holding: bool


def reinstall_reason(health: HookHealth) -> str | None:
    """Why the hook should be re-installed right now, or ``None`` to keep it.

    Re-installing a healthy hook is cheap, so the rules err towards doing it:
    every notification that marks a moment when Windows is known to drop
    hooks is taken at face value, and no attempt is made to check whether the
    hook is still alive first.

    The backstop is the careful part. It never fires because the keyboard has
    been quiet - it fires only when Windows saw input that our hook did not,
    which cannot happen while the machine is idle.
    """
    if health.wake_pending:
        return "wake"
    if health.holding:
        # The hold exists only because the hook handed us the key-down, so it
        # is proof of life; and cutting a take short mid-word would be a
        # worse bug than a hook re-armed a minute later.
        return None
    if health.now - health.installed_at < INSTALL_GRACE_SEC:
        return None
    if health.last_input_at is None:
        return None
    # A hook can only have seen what happened after it was installed, so an
    # install that has heard nothing yet is judged from its own age.
    seen_at = health.installed_at
    if health.last_hook_event_at is not None and health.last_hook_event_at > seen_at:
        seen_at = health.last_hook_event_at
    if health.last_input_at - seen_at > MISSED_INPUT_TOLERANCE_SEC:
        return "silent"
    return None


class HotkeyListener:
    """Press-and-hold detector for one configurable combination.

    ``on_press`` fires once per physical press (auto-repeat is suppressed) and
    ``on_release`` fires when the key or any required modifier goes up. Both
    run on the hook thread - marshal to the GUI thread on the receiving side.

    A modifier-only hotkey (right Ctrl held on its own) is driven by the
    modifier itself and is passed through to the focused application, so the
    key keeps doing its usual job in Ctrl+C. When a regular key joins it the
    hold was a shortcut after all and ``on_cancel`` fires instead of
    ``on_release``, so nothing gets transcribed.

    The hook re-arms itself after sleep, hibernation and the lock screen (see
    ``reinstall_reason``). If a re-install ever fails the hotkey is really
    gone, ``is_running`` goes False and ``on_hook_lost`` is called with a
    message for the user - staying silent there is the very failure this
    machinery exists to prevent.
    """

    def __init__(
        self,
        hotkey: Hotkey,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
        is_key_down: Optional[Callable[[int], bool]] = None,
        on_hook_lost: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        # Fired instead of on_release when the take turns out to be a
        # shortcut (right Ctrl held for Ctrl+C) - nothing said belongs to a
        # dictation then. Without a handler of its own the take is simply
        # stopped, which is still better than transcribing it.
        self._on_cancel = on_cancel if on_cancel is not None else on_release
        self._is_key_down = is_key_down if is_key_down is not None else _key_is_down
        # Called from the hook thread when the hotkey is dead for good. The
        # default only logs, so existing call sites keep working - but then
        # nobody tells the user.
        self._on_hook_lost = on_hook_lost if on_hook_lost is not None else _log_hook_lost
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
        # Health bookkeeping. Written on the hook thread, read on the message
        # loop of the same thread, so plain attributes are enough.
        self._now: Callable[[], float] = time.monotonic
        self._input_activity_at: Callable[[], float | None] = self._system_input_at
        self._installed_at: float = 0.0
        self._last_event_at: float | None = None
        self._wake_pending = False
        self._hwnd = None
        self._wndproc = None  # live reference to the WNDPROC, do not drop
        self._wts_registered = False
        self._power_handle = None
        self._watch_class: str | None = None
        self._wtsapi32 = None

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
        if self._thread is not None and not self._thread.is_alive():
            # The hook thread ended on its own - a re-install failed after a
            # wake, most likely. It holds no hook any more, so a fresh start
            # is safe and is exactly what the retry from the tray needs.
            self._thread = None
            self._thread_id = 0
            self._active = False
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
            if not self._install_hook():
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
            # Without the window the hook still works, it just stops re-arming
            # itself - that is a degraded hotkey, not a dead one.
            self._create_watch_window()
            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT or error
                    break
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("Keyboard hook message loop failed")
        finally:
            self._destroy_watch_window()
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._proc = None

    def _install_hook(self) -> int:  # pragma: no cover - Windows only
        """Install a fresh hook; the HOOKPROC is swapped in only on success.

        A HOOKPROC that gets garbage collected while its hook is installed
        takes the whole process down with it, so the reference lives in
        ``_proc`` and is only replaced once the new hook is really in place.
        """
        proc = HOOKPROC(self._callback)
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, None, 0)
        if not handle:
            return 0
        self._proc = proc
        self._hook = handle
        self._installed_at = self._now()
        self._last_event_at = None
        return handle

    def _reinstall_hook(self, reason: str) -> bool:  # pragma: no cover - Windows
        """Take the old hook down and put a new one up. Never two at once."""
        old_hook, old_proc = self._hook, self._proc  # noqa: F841 - keeps it alive
        self._hook = None
        if old_hook and not user32.UnhookWindowsHookEx(old_hook):
            log.warning("Unhook before re-install failed: %s", ctypes.get_last_error())
        # The unhook has returned, so no callback of the old HOOKPROC can be
        # running any more and ``old_proc`` may be dropped when this returns.
        self._drop_take()
        if self._install_hook():
            log.info("Keyboard hook re-installed (%s)", reason)
            return True
        self._report_reinstall_failure(reason, ctypes.get_last_error())
        # Leave the loop: the thread holds nothing now, and a dead thread is
        # what lets start() try again from the tray.
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        return False

    def _report_reinstall_failure(self, reason: str, code: int) -> None:
        """The hotkey is really gone - say so instead of dying quietly."""
        log.error("Keyboard hook re-install failed (%s): Windows error %s", reason, code)
        self._proc = None
        self._active = False
        self._hook_lost(
            f"Горячая клавиша перестала работать (ошибка Windows {code}). "
            "Нажмите на значок в трее, чтобы включить её заново"
        )

    # ------------------------------------------------------------- watchdog
    def _create_watch_window(self) -> bool:  # pragma: no cover - Windows only
        """Message-only window that hears about wakes, unlocks and the timer."""
        try:
            self._wndproc = WNDPROC(self._watch_wndproc)
            instance = kernel32.GetModuleHandleW(None)
            # A class name of its own per window: re-registering a name whose
            # WNDPROC belonged to a previous listener would point Windows at a
            # freed callback.
            name = f"GovorilkaHookWatch{next(_watch_class_seq)}"
            window_class = WNDCLASSEXW()
            window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
            window_class.lpfnWndProc = self._wndproc
            window_class.hInstance = instance
            window_class.lpszClassName = name
            if not user32.RegisterClassExW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._watch_class = name
            hwnd = user32.CreateWindowExW(
                0, name, name, 0, 0, 0, 0, 0, HWND_MESSAGE, None, instance, None
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            self._register_notifications(hwnd)
            if not user32.SetTimer(hwnd, HEALTH_TIMER_ID, HEALTH_TIMER_MS, None):
                log.warning("Hook health timer could not be set: %s",
                            ctypes.get_last_error())
            return True
        except Exception:
            log.exception("Hook watchdog window failed; the hook will not re-arm")
            return False

    def _register_notifications(self, hwnd) -> None:  # pragma: no cover - Windows
        try:
            register = user32.RegisterSuspendResumeNotification
            register.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            register.restype = wintypes.HANDLE
            # A message-only window is never sent the broadcast form of
            # WM_POWERBROADCAST, so it has to be asked for by handle.
            self._power_handle = register(hwnd, DEVICE_NOTIFY_WINDOW_HANDLE)
            if not self._power_handle:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            log.exception("Resume notifications unavailable, timer only")
            self._power_handle = None
        try:
            self._wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
            self._wtsapi32.WTSRegisterSessionNotification.argtypes = [
                wintypes.HWND, wintypes.DWORD
            ]
            self._wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
            self._wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            self._wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            if not self._wtsapi32.WTSRegisterSessionNotification(
                hwnd, NOTIFY_FOR_THIS_SESSION
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._wts_registered = True
        except Exception:
            log.exception("Session notifications unavailable, timer only")
            self._wts_registered = False

    def _destroy_watch_window(self) -> None:  # pragma: no cover - Windows only
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.KillTimer(hwnd, HEALTH_TIMER_ID)
                if self._wts_registered:
                    self._wtsapi32.WTSUnRegisterSessionNotification(hwnd)
                if self._power_handle:
                    user32.UnregisterSuspendResumeNotification(self._power_handle)
                user32.DestroyWindow(hwnd)
            except Exception:
                log.exception("Hook watchdog window cleanup failed")
        if self._watch_class:
            try:
                instance = kernel32.GetModuleHandleW(None)
                user32.UnregisterClassW(self._watch_class, instance)
            except Exception:
                log.exception("Hook watchdog class cleanup failed")
            self._watch_class = None
        self._hwnd = None
        self._power_handle = None
        self._wts_registered = False
        self._wndproc = None

    def _watch_wndproc(self, hwnd, msg, w_param, l_param):  # pragma: no cover
        """Runs on the message loop - never on the hook callback."""
        try:
            if msg == WM_TIMER and w_param == HEALTH_TIMER_ID:
                self._check_health()
            elif msg == WM_POWERBROADCAST and w_param in (
                PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND
            ):
                log.info("Resume notification: re-arming the keyboard hook")
                self._wake_pending = True
                self._check_health()
            elif msg == WM_WTSSESSION_CHANGE and w_param in (
                WTS_SESSION_UNLOCK, WTS_SESSION_LOGON
            ):
                log.info("Session unlocked: re-arming the keyboard hook")
                self._wake_pending = True
                self._check_health()
        except Exception:
            log.exception("Hook watchdog failed")
        return user32.DefWindowProcW(hwnd, msg, w_param, l_param)

    # --------------------------------------------------------------- health
    def _health_snapshot(self) -> HookHealth:
        now = self._now()
        return HookHealth(
            now=now,
            installed_at=self._installed_at,
            last_hook_event_at=self._last_event_at,
            last_input_at=self._input_activity_at(),
            wake_pending=self._wake_pending,
            holding=self._held,
        )

    def _check_health(self) -> None:
        """Re-install if the state of things says so. Message loop only."""
        reason = reinstall_reason(self._health_snapshot())
        self._wake_pending = False
        if reason is None:
            return
        self._reinstall_hook(reason)

    def _drop_take(self) -> None:
        """Forget the keyboard state a lost hook left behind.

        Nothing tracked as held is really held by now: the key-up went to the
        hook that Windows took away. A take that survived the sleep is
        dropped rather than transcribed - the machine has been away, and what
        was recorded is not what the user meant to dictate.
        """
        with self._lock:
            was_held = self._held
            self._held = False
            self._pressed_modifier_vks.clear()
            self._down_keys.clear()
        if was_held:
            log.warning("A dictation was in flight when the hook was re-armed")
            self._fire(self._on_cancel)

    def _hook_lost(self, message: str) -> None:
        try:
            self._on_hook_lost(message)
        except Exception:
            log.exception("Hook loss handler failed")

    def _system_input_at(self) -> float | None:
        """When Windows itself last saw input, on the clock of ``_now``."""
        if not _IS_WINDOWS:
            return None
        try:  # pragma: no cover - Windows only
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            # Both are 32-bit tick counts, so the subtraction is masked back
            # to that width and survives the 49-day wrap.
            idle_ms = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
            return self._now() - idle_ms / 1000.0
        except Exception:
            log.exception("GetLastInputInfo failed")
            return None

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
        # One clock read: proof that Windows is still feeding this hook. It
        # is taken before the injected check, because an injected event
        # arrived through the hook just the same.
        self._last_event_at = self._now()
        if flags & LLKHF_INJECTED:
            # Our own SendInput (paste, modifier releases) and macro tools come
            # back through this hook; they must not move the state machine.
            return False

        swallow, callbacks = self._decide(vk, message)
        for callback in callbacks:
            self._fire(callback)
        return swallow

    def _decide(
        self, vk: int, message: int
    ) -> tuple[bool, list[Callable[[], None]]]:
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP)
        if not (is_down or is_up):
            return False, []

        with self._lock:
            hotkey = self._hotkey
            recovered = self._forget_lost_modifiers(vk)

            if vk in ALL_MODIFIER_VKS:
                if is_down:
                    repeat = vk in self._pressed_modifier_vks
                    self._pressed_modifier_vks.add(vk)
                    if (
                        hotkey.is_modifier_only
                        and vk == hotkey.trigger_vk
                        and not repeat
                        and not self._held
                        and self._modifiers_satisfied(hotkey)
                    ):
                        self._held = True
                        # Never swallowed: the key must keep working as a
                        # normal modifier, or every shortcut on that side of
                        # the keyboard dies.
                        return False, recovered + [self._on_press]
                    return False, recovered
                self._pressed_modifier_vks.discard(vk)
                # Releasing a required modifier ends the hold; a foreign one
                # (Shift under Ctrl+Alt+Space) leaves the hold alone.
                if self._held and vk in hotkey.all_modifier_vks():
                    self._held = False
                    return False, recovered + [self._on_release]
                return False, recovered

            if is_down:
                repeat = vk in self._down_keys
                self._down_keys.add(vk)
            else:
                repeat = False
                self._down_keys.discard(vk)

            if is_down and self._held and hotkey.is_modifier_only:
                # The modifier turned out to be the modifier of a shortcut:
                # right Ctrl plus C is a copy, not a dictation. Drop the take
                # instead of transcribing whatever was said before it.
                self._held = False
                return False, recovered + [self._on_cancel]

            if vk != hotkey.key_vk:
                return False, recovered

            if is_down:
                if self._held:
                    return True, recovered  # auto-repeat: swallow, do not re-fire
                if repeat:
                    # The key was already down before this combination was
                    # armed - wait for a real press instead of a phantom one.
                    return False, recovered
                if not self._modifiers_satisfied(hotkey):
                    return False, recovered
                self._held = True
                return True, recovered + [self._on_press]

            # key up
            if self._held:
                self._held = False
                return True, recovered + [self._on_release]
            return False, recovered

    def _forget_lost_modifiers(self, current_vk: int) -> list[Callable[[], None]]:
        """Drop modifiers Windows no longer sees as held.

        A key-up can go missing: a UAC prompt or a hook above ours swallows
        it, and the tracked set then keeps a key that has long been up, so the
        hotkey never arms again. The key of the event in hand is left alone -
        the branches below are what decide its meaning.
        """
        stale = {
            vk
            for vk in self._pressed_modifier_vks
            if vk != current_vk and not self._key_looks_held(vk)
        }
        if not stale:
            return []
        log.warning("Modifiers held in our state but up in Windows: %s", sorted(stale))
        self._pressed_modifier_vks -= stale
        if self._held and stale & self._hotkey.all_modifier_vks():
            self._held = False
            return [self._on_release]
        return []

    def _key_looks_held(self, vk: int) -> bool:
        """True when the key looks held; any failure keeps the tracked state."""
        try:
            return bool(self._is_key_down(vk))
        except Exception:
            log.exception("Key state check failed")
            return True

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


def _log_hook_lost(message: str) -> None:
    """Default for ``on_hook_lost``: better than nothing, worse than the tray."""
    log.error("Hotkey lost and not re-armed: %s", message)


def _key_is_down(vk: int) -> bool:
    """Physical state of a key, or "held" where Windows cannot be asked.

    GetAsyncKeyState answers about the hardware right now, which is what the
    reconciliation needs: the state machine may have missed the key-up long
    ago. It is a seam so the decision logic stays testable off Windows.
    """
    if not _IS_WINDOWS:
        return True
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)  # pragma: no cover


def _family_of_vk(vk: int) -> str | None:
    for name, vks in MODIFIER_VKS.items():
        if vk in vks:
            return MODIFIER_FAMILY[name]
    return None
