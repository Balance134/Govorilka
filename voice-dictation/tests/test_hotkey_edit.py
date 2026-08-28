"""The chord state machine of the hotkey capture field.

PySide6 is not importable here and must never be required by the suite, so a
minimal fake of the three Qt pieces the widget uses (Qt constants, Signal,
QLineEdit) is installed before the import. Everything below then drives the
widget with fake key events, exactly as Windows would deliver them.
"""

import sys
import types

if "PySide6" not in sys.modules:  # pragma: no cover - depends on the box

    class _Qt:
        # Real Qt values, so the fixtures read like the ones Windows sends.
        Key_Escape = 0x01000000
        Key_Tab = 0x01000001
        Key_Backspace = 0x01000003
        Key_Return = 0x01000004
        Key_Enter = 0x01000005
        Key_Insert = 0x01000006
        Key_Delete = 0x01000007
        Key_Home = 0x01000010
        Key_End = 0x01000011
        Key_Left = 0x01000012
        Key_Up = 0x01000013
        Key_Right = 0x01000014
        Key_Down = 0x01000015
        Key_PageUp = 0x01000016
        Key_PageDown = 0x01000017
        Key_Shift = 0x01000020
        Key_Control = 0x01000021
        Key_Meta = 0x01000022
        Key_Alt = 0x01000023
        Key_CapsLock = 0x01000024
        Key_AltGr = 0x01001103
        Key_Space = 0x20
        ShiftModifier = 0x02000000
        ControlModifier = 0x04000000
        AltModifier = 0x08000000
        MetaModifier = 0x10000000

    for _i in range(1, 25):
        setattr(_Qt, f"Key_F{_i}", 0x01000029 + _i)

    class _BoundSignal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class _Signal:
        """Class attribute that hands out one bound signal per instance."""

        def __init__(self, *types_):
            self._name = "signal"

        def __set_name__(self, owner, name):
            self._name = name

        def __get__(self, obj, owner=None):
            if obj is None:
                return self
            bound = obj.__dict__.setdefault("_bound_signals", {})
            return bound.setdefault(self._name, _BoundSignal())

    class _QLineEdit:
        def __init__(self, parent=None):
            self._text = ""
            self._read_only = False
            self._placeholder = ""

        def setReadOnly(self, value):  # noqa: N802 - Qt naming
            self._read_only = value

        def setPlaceholderText(self, value):  # noqa: N802 - Qt naming
            self._placeholder = value

        def setText(self, value):  # noqa: N802 - Qt naming
            self._text = value

        def text(self):
            return self._text

        def focusOutEvent(self, event):  # noqa: N802 - Qt naming
            pass

    core = types.ModuleType("PySide6.QtCore")
    core.Qt = _Qt
    core.Signal = _Signal
    gui = types.ModuleType("PySide6.QtGui")
    gui.QKeyEvent = object
    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QLineEdit = _QLineEdit
    package = types.ModuleType("PySide6")
    package.QtCore = core
    package.QtGui = gui
    package.QtWidgets = widgets
    sys.modules["PySide6"] = package
    sys.modules["PySide6.QtCore"] = core
    sys.modules["PySide6.QtGui"] = gui
    sys.modules["PySide6.QtWidgets"] = widgets

from PySide6.QtCore import Qt  # noqa: E402

from src.ui.hotkey_edit import HotkeyEdit  # noqa: E402

VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

SC_CTRL = 0x1D
SC_ALT = 0x38
SC_EXTENDED = 0x100  # Qt keeps the extended flag in bit 8 of nativeScanCode


class FakeKeyEvent:
    """What QKeyEvent gives the widget, and nothing more."""

    def __init__(self, key, vk=0, scancode=0, modifiers=0, text=""):
        self._key = key
        self._vk = vk
        self._scancode = scancode
        self._modifiers = modifiers
        self._text = text
        self.accepted = False

    def key(self):
        return self._key

    def nativeVirtualKey(self):  # noqa: N802 - Qt naming
        return self._vk

    def nativeScanCode(self):  # noqa: N802 - Qt naming
        return self._scancode

    def modifiers(self):
        return self._modifiers

    def text(self):
        return self._text

    def accept(self):
        self.accepted = True


def make_edit(text="ctrl+alt+space"):
    edit = HotkeyEdit(text)
    failures: list[str] = []
    edit.captureFailed.connect(failures.append)
    return edit, failures


def rctrl_event():
    return FakeKeyEvent(Qt.Key_Control, VK_RCONTROL, SC_CTRL | SC_EXTENDED)


def lalt_event():
    return FakeKeyEvent(Qt.Key_Alt, VK_LMENU, SC_ALT)


def tap(edit, event_factory):
    edit.keyPressEvent(event_factory())
    edit.keyReleaseEvent(event_factory())


# --------------------------------------------------- the key the owner asked for
def test_a_right_ctrl_tap_commits_rctrl():
    edit, failures = make_edit()
    tap(edit, rctrl_event)
    assert edit.hotkey_text() == "rctrl"
    assert failures == []


def test_a_left_alt_tap_commits_lalt():
    edit, failures = make_edit()
    tap(edit, lalt_event)
    assert edit.hotkey_text() == "lalt"
    assert failures == []


# ----------------------------------------- #1 a lost modifier key-up must heal
def test_a_lost_alt_key_up_does_not_brick_the_field():
    """Alt+Tab: Windows eats the Alt key-up and the focus goes away."""
    edit, failures = make_edit()
    edit.keyPressEvent(lalt_event())  # the release never arrives
    edit.focusOutEvent(object())

    tap(edit, rctrl_event)
    assert edit.hotkey_text() == "rctrl"
    assert failures == []


def test_focus_loss_restores_the_previous_value():
    edit, _ = make_edit()
    edit.keyPressEvent(lalt_event())
    assert edit.text().endswith("…")
    edit.focusOutEvent(object())
    assert edit.hotkey_text() == "ctrl+alt+space"


def test_reopening_the_settings_recovers_a_stuck_field():
    """SettingsWindow is reused, so load_config must clear the chord too."""
    edit, failures = make_edit()
    edit.keyPressEvent(lalt_event())  # key-up swallowed, no focus change

    edit.set_hotkey_text("ctrl+alt+space")  # what load_config does
    assert edit.hotkey_text() == "ctrl+alt+space"
    tap(edit, rctrl_event)
    assert edit.hotkey_text() == "rctrl"
    assert failures == []


# -------------------------------- #2 a stray letter must not kill the capture
def test_a_stray_letter_does_not_disable_the_next_capture():
    edit, failures = make_edit()
    edit.keyPressEvent(FakeKeyEvent(ord("A"), 0x41, 0x1E, text="a"))
    assert failures == ["Нужен хотя бы один модификатор (Ctrl, Alt, Shift или Win)"]

    tap(edit, rctrl_event)
    assert edit.hotkey_text() == "rctrl"
    assert failures == ["Нужен хотя бы один модификатор (Ctrl, Alt, Shift или Win)"]


def test_a_combination_still_commits_after_a_stray_letter():
    edit, _ = make_edit("rctrl")
    edit.keyPressEvent(FakeKeyEvent(ord("A"), 0x41, 0x1E, text="a"))
    edit.keyPressEvent(FakeKeyEvent(Qt.Key_Control, VK_LCONTROL, SC_CTRL))
    edit.keyPressEvent(
        FakeKeyEvent(Qt.Key_Space, 0x20, 0x39, modifiers=Qt.ControlModifier)
    )
    assert edit.hotkey_text() == "ctrl+space"


# --------------------------------------------------------------- #5 AltGr
def test_an_altgr_tap_commits_ralt():
    """AltGr sends a synthetic left Ctrl before the right Alt."""
    edit, failures = make_edit()
    edit.keyPressEvent(FakeKeyEvent(Qt.Key_Control, VK_LCONTROL, SC_CTRL))
    edit.keyPressEvent(
        FakeKeyEvent(Qt.Key_AltGr, VK_RMENU, SC_ALT | SC_EXTENDED)
    )
    edit.keyReleaseEvent(FakeKeyEvent(Qt.Key_Control, VK_LCONTROL, SC_CTRL))
    edit.keyReleaseEvent(
        FakeKeyEvent(Qt.Key_AltGr, VK_RMENU, SC_ALT | SC_EXTENDED)
    )
    assert edit.hotkey_text() == "ralt"
    assert failures == []


# -------------------------------------- an unresolved side is said out loud
def test_a_modifier_without_a_side_asks_which_one():
    edit, failures = make_edit()
    # Neither the virtual key nor the scan code names a side, and GetKeyState
    # is unavailable off Windows.
    tap(edit, lambda: FakeKeyEvent(Qt.Key_Control))
    assert failures == ["Укажите, какая именно клавиша: rctrl или lctrl — "
                        "иначе диктовка сработает и на второй, а её занимают "
                        "обычные сочетания Windows"]
    assert edit.hotkey_text() == "ctrl+alt+space"  # the old value is back


def test_a_windows_key_alone_is_refused_with_a_reason():
    edit, failures = make_edit()
    tap(edit, lambda: FakeKeyEvent(Qt.Key_Meta, 0x5B, 0x5B))
    assert failures and "меню «Пуск»" in failures[0]
    assert edit.hotkey_text() == "ctrl+alt+space"


# ------------------------------------------------------------- housekeeping
def test_caps_lock_is_not_a_hotkey():
    edit, failures = make_edit()
    tap(edit, lambda: FakeKeyEvent(Qt.Key_CapsLock, 0x14, 0x3A))
    assert edit.hotkey_text() == "ctrl+alt+space"
    assert failures == []


def test_two_modifiers_are_refused_out_loud():
    edit, failures = make_edit()
    edit.keyPressEvent(rctrl_event())
    edit.keyPressEvent(lalt_event())
    edit.keyReleaseEvent(rctrl_event())
    edit.keyReleaseEvent(lalt_event())
    assert edit.hotkey_text() == "ctrl+alt+space"
    assert failures == [
        "Как горячая клавиша подходит либо один модификатор, "
        "либо сочетание с обычной клавишей"
    ]


def test_a_combination_with_a_regular_key_is_captured():
    edit, _ = make_edit("rctrl")
    edit.keyPressEvent(lalt_event())
    edit.keyPressEvent(
        FakeKeyEvent(Qt.Key_Space, 0x20, 0x39, modifiers=Qt.AltModifier)
    )
    edit.keyReleaseEvent(lalt_event())
    assert edit.hotkey_text() == "alt+space"
