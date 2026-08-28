"""Decision logic of the keyboard hook, driven with plain integers.

Nothing here touches Win32: the listener is built but never started, and only
``_handle_key`` (the pure part of the callback) is exercised.
"""

import threading

import pytest

from src.hotkey import listener as listener_module
from src.hotkey.listener import (
    LLKHF_INJECTED,
    WM_KEYDOWN,
    WM_KEYUP,
    HotkeyListener,
)
from src.hotkey.parser import (
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_RCONTROL,
    VK_RSHIFT,
    parse,
)

VK_SPACE = 0x20
VK_F9 = 0x78
VK_A = 0x41


class Recorder:
    def __init__(self):
        self.events: list[str] = []

    def press(self):
        self.events.append("press")

    def release(self):
        self.events.append("release")


def make_listener(text="ctrl+alt+space"):
    events = Recorder()
    listener = HotkeyListener(parse(text), events.press, events.release)
    return listener, events


def down(listener, vk, flags=0):
    return listener._handle_key(vk, WM_KEYDOWN, flags)


def up(listener, vk, flags=0):
    return listener._handle_key(vk, WM_KEYUP, flags)


# ------------------------------------------------------------- baseline
def test_press_and_release_fire_once_and_are_swallowed():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    assert down(listener, VK_SPACE) is True
    assert down(listener, VK_SPACE) is True  # auto-repeat
    assert events.events == ["press"]
    assert up(listener, VK_SPACE) is True
    assert events.events == ["press", "release"]


def test_key_without_modifiers_is_passed_through():
    listener, events = make_listener()
    assert down(listener, VK_SPACE) is False
    assert events.events == []


def test_releasing_a_required_modifier_ends_the_hold():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_SPACE)
    up(listener, VK_LMENU)
    assert events.events == ["press", "release"]


# ------------------------------------- item 1: foreign modifiers are tracked
def test_extra_modifier_family_blocks_the_hotkey():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_LSHIFT)
    assert down(listener, VK_SPACE) is False
    assert events.events == []


def test_hotkey_works_again_after_the_extra_modifier_is_released():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_LSHIFT)
    down(listener, VK_SPACE)
    up(listener, VK_SPACE)
    up(listener, VK_LSHIFT)
    assert down(listener, VK_SPACE) is True
    assert events.events == ["press"]


def test_releasing_a_foreign_modifier_does_not_end_the_hold():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_SPACE)
    down(listener, VK_RSHIFT)
    up(listener, VK_RSHIFT)
    assert events.events == ["press"]
    up(listener, VK_SPACE)
    assert events.events == ["press", "release"]


# --------------------------------------------- item 3: injected input ignored
def test_injected_key_does_not_start_dictation():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    assert down(listener, VK_SPACE, flags=LLKHF_INJECTED) is False
    assert events.events == []


def test_injected_modifier_does_not_change_the_tracked_state():
    listener, events = make_listener()
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_SPACE)
    # The app's own SendInput releases the modifiers before pasting.
    up(listener, VK_LCONTROL, flags=LLKHF_INJECTED)
    up(listener, VK_LMENU, flags=LLKHF_INJECTED)
    assert events.events == ["press"]
    assert listener._pressed_modifier_vks == {VK_LCONTROL, VK_LMENU}


# ------------------------------------ item 5: callbacks fire outside the lock
def test_callback_runs_without_the_lock_held():
    listener, events = make_listener()
    seen: list[bool] = []

    def on_press():
        seen.append(listener._lock.acquire(blocking=False))
        if seen[-1]:
            listener._lock.release()
        events.press()

    listener._on_press = on_press
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    down(listener, VK_SPACE)
    assert seen == [True]


def test_set_hotkey_is_not_blocked_by_a_slow_callback():
    listener, events = make_listener()
    inside = threading.Event()
    finished = threading.Event()

    def on_press():
        inside.set()
        finished.wait(timeout=10.0)
        events.press()

    listener._on_press = on_press
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    worker = threading.Thread(target=lambda: down(listener, VK_SPACE))
    worker.start()
    assert inside.wait(timeout=2.0)
    switched = threading.Thread(target=listener.set_hotkey, args=(parse("ctrl+f9"),))
    switched.start()
    switched.join(timeout=1.0)
    assert not switched.is_alive()  # the GUI thread was not stuck on the lock
    finished.set()
    worker.join(timeout=2.0)


# ------------------------------- item 6: no phantom start after a hotkey swap
def test_switching_to_a_bare_key_does_not_arm_on_auto_repeat():
    listener, events = make_listener("ctrl+f9")
    down(listener, VK_LCONTROL)
    down(listener, VK_F9)
    assert events.events == ["press"]

    listener.set_hotkey(parse("f9"))
    assert events.events == ["press", "release"]
    up(listener, VK_LCONTROL)  # Ctrl is no longer part of the combination

    down(listener, VK_F9)  # auto-repeat of the key still physically held
    down(listener, VK_F9)
    assert events.events == ["press", "release"]

    up(listener, VK_F9)
    assert down(listener, VK_F9) is True
    assert events.events == ["press", "release", "press"]


def test_key_held_before_arming_needs_a_real_press():
    listener, events = make_listener()
    down(listener, VK_SPACE)  # space alone, nothing armed
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    assert down(listener, VK_SPACE) is False  # still the same physical press
    assert events.events == []
    up(listener, VK_SPACE)
    assert down(listener, VK_SPACE) is True
    assert events.events == ["press"]


# ------------------------------------------------------------- housekeeping
def test_other_keys_are_ignored():
    listener, events = make_listener()
    assert down(listener, VK_A) is False
    assert up(listener, VK_A) is False
    assert events.events == []


def test_callback_exception_does_not_break_the_hook():
    listener, _ = make_listener()
    listener._on_press = lambda: 1 / 0
    down(listener, VK_LCONTROL)
    down(listener, VK_LMENU)
    assert down(listener, VK_SPACE) is True
    assert up(listener, VK_SPACE) is True


# ------------------------------------------------- #1 one hook and one only
def arm_for_windows(monkeypatch, listener, run) -> None:
    """Lets start() run its Windows path with a fake hook thread."""
    monkeypatch.setattr(listener_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(listener_module, "HOOK_READY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(listener, "_run", run)


def test_install_timeout_keeps_the_thread_and_refuses_a_second_start(monkeypatch):
    runs = []
    release = threading.Event()
    listener, _ = make_listener()

    def slow_run():
        runs.append(1)  # never reports back in time
        release.wait(5)

    arm_for_windows(monkeypatch, listener, slow_run)
    try:
        with pytest.raises(RuntimeError, match="не запустился"):
            listener.start()
        # The thread is still alive and may install the hook a moment later:
        # it is told to unhook itself and kept, so nothing installs a second.
        assert listener._thread is not None
        assert listener._cancelled.is_set()
        assert listener.is_running is False

        with pytest.raises(RuntimeError, match="Перезапустите программу"):
            listener.start()
        assert runs == [1]  # no second hook thread was started
    finally:
        release.set()


def test_a_started_listener_does_not_start_twice(monkeypatch):
    runs = []
    release = threading.Event()
    listener, _ = make_listener()

    def run():
        runs.append(1)
        listener._ready.set()
        release.wait(5)

    arm_for_windows(monkeypatch, listener, run)
    try:
        listener.start()
        assert listener.is_running is True
        listener.start()  # idempotent, not a second hook
        assert runs == [1]
    finally:
        release.set()


def test_a_failed_installation_can_be_retried(monkeypatch):
    runs = []
    listener, _ = make_listener()

    def run():
        runs.append(1)
        listener._install_error = "Не удалось перехватить клавиатуру"
        listener._ready.set()

    arm_for_windows(monkeypatch, listener, run)
    with pytest.raises(RuntimeError, match="перехватить клавиатуру"):
        listener.start()
    # The thread is gone and no hook was installed, so a retry is safe.
    assert listener._thread is None
    assert listener.is_running is False
    with pytest.raises(RuntimeError):
        listener.start()
    assert runs == [1, 1]


# ------------------------------------- a single side-specific modifier hotkey
def test_a_held_modifier_fires_press_and_release_once():
    listener, events = make_listener("rctrl")
    assert down(listener, VK_RCONTROL) is False
    assert events.events == ["press"]
    assert up(listener, VK_RCONTROL) is False
    assert events.events == ["press", "release"]


def test_auto_repeat_of_a_modifier_does_not_fire_again():
    listener, events = make_listener("rctrl")
    down(listener, VK_RCONTROL)
    down(listener, VK_RCONTROL)
    down(listener, VK_RCONTROL)
    assert events.events == ["press"]
    up(listener, VK_RCONTROL)
    assert events.events == ["press", "release"]


def test_a_modifier_hotkey_is_never_swallowed():
    """Right Ctrl must keep working as Ctrl in Ctrl+C."""
    listener, _ = make_listener("rctrl")
    assert down(listener, VK_RCONTROL) is False
    assert down(listener, VK_A) is False  # rctrl+A reaches the application
    assert up(listener, VK_A) is False
    assert up(listener, VK_RCONTROL) is False
    # A combination with a regular key is still swallowed.
    combo, _ = make_listener()
    down(combo, VK_LCONTROL)
    down(combo, VK_LMENU)
    assert down(combo, VK_SPACE) is True


def test_the_other_side_of_the_family_does_not_fire():
    listener, events = make_listener("rctrl")
    assert down(listener, VK_LCONTROL) is False
    assert events.events == []


def test_an_extra_modifier_family_blocks_the_modifier_hotkey():
    listener, events = make_listener("rctrl")
    down(listener, VK_LSHIFT)
    assert down(listener, VK_RCONTROL) is False
    assert events.events == []
    up(listener, VK_RCONTROL)
    assert events.events == []


def test_the_modifier_hotkey_works_after_the_extra_modifier_is_released():
    listener, events = make_listener("rctrl")
    down(listener, VK_LSHIFT)
    down(listener, VK_RCONTROL)
    up(listener, VK_RCONTROL)
    up(listener, VK_LSHIFT)
    down(listener, VK_RCONTROL)
    assert events.events == ["press"]


def test_a_regular_key_pressed_under_the_modifier_keeps_the_hold():
    listener, events = make_listener("rctrl")
    down(listener, VK_RCONTROL)
    down(listener, VK_A)
    up(listener, VK_A)
    assert events.events == ["press"]
    up(listener, VK_RCONTROL)
    assert events.events == ["press", "release"]


def test_switching_to_a_modifier_hotkey_does_not_arm_on_auto_repeat():
    listener, events = make_listener()
    down(listener, VK_RCONTROL)  # already physically held
    listener.set_hotkey(parse("rctrl"))
    down(listener, VK_RCONTROL)  # auto-repeat of that same press
    assert events.events == []
    up(listener, VK_RCONTROL)
    assert down(listener, VK_RCONTROL) is False
    assert events.events == ["press"]
