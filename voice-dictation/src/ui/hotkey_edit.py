"""Read-only field that records a key combination the user presses."""

from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLineEdit

from ..hotkey.parser import (
    FAMILY_SIDE_VKS,
    Hotkey,
    HotkeyError,
    collapse_altgr,
    from_parts,
    key_token_to_vk,
    parse,
    resolve_modifier_side,
    side_required_message,
)

log = logging.getLogger(__name__)

KEY_DOWN_BIT = 0x8000

_MODIFIER_KEYS = {
    Qt.Key_Control,
    Qt.Key_Alt,
    Qt.Key_AltGr,
    Qt.Key_Shift,
    Qt.Key_Meta,
    Qt.Key_CapsLock,
}

# Qt key -> parser token, used when nativeVirtualKey is unavailable.
_QT_KEY_TOKENS = {
    Qt.Key_Space: "space",
    Qt.Key_Return: "enter",
    Qt.Key_Enter: "enter",
    Qt.Key_Tab: "tab",
    Qt.Key_Escape: "esc",
    Qt.Key_Backspace: "backspace",
    Qt.Key_Delete: "delete",
    Qt.Key_Insert: "insert",
    Qt.Key_Home: "home",
    Qt.Key_End: "end",
    Qt.Key_PageUp: "pageup",
    Qt.Key_PageDown: "pagedown",
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
}
for _i in range(1, 25):
    _QT_KEY_TOKENS[getattr(Qt, f"Key_F{_i}")] = f"f{_i}"

# Modifier family of a Qt key, so we know which pair of virtual keys to ask
# Windows about. Caps Lock is deliberately absent - it is no hotkey.
_QT_KEY_FAMILIES = {
    Qt.Key_Control: "ctrl",
    Qt.Key_Alt: "alt",
    Qt.Key_AltGr: "alt",
    Qt.Key_Shift: "shift",
    Qt.Key_Meta: "win",
}


def _load_user32():
    """user32 for the key-state check; None everywhere it is unavailable."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetKeyState.argtypes = [ctypes.c_int]
        user32.GetKeyState.restype = ctypes.c_short
        return user32
    except Exception:  # pragma: no cover - Windows only
        log.exception("Cannot load user32, key side will be guessed from the event")
        return None


_user32 = _load_user32()


def _held_sides(family: str) -> tuple[str, ...]:
    """Which sides of a family Windows sees as held down right now.

    GetKeyState (not GetAsyncKeyState) on purpose: it answers for the message
    being processed, which is exactly the key event in hand, so a key already
    released by the time Qt delivers the event cannot mislead us.
    """
    if _user32 is None:
        return ()
    try:  # pragma: no cover - Windows only
        return tuple(
            name
            for name, vk in FAMILY_SIDE_VKS[family]
            if _user32.GetKeyState(vk) & KEY_DOWN_BIT
        )
    except Exception:  # pragma: no cover - Windows only
        log.exception("GetKeyState failed")
        return ()


class HotkeyEdit(QLineEdit):
    """Shows a combination string; typing into it is impossible by design."""

    captureFailed = Signal(str)

    def __init__(self, hotkey_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Нажмите нужное сочетание")
        self._hotkey: Hotkey | None = None
        # State of the combination being held right now, reset once every
        # modifier is up again.
        self._modifiers_down: set[int] = set()
        self._chord_modifiers: list[str] = []
        self._chord_unnamed_families: list[str] = []
        self._chord_has_unknown_modifier = False
        self._chord_has_regular_key = False
        self.set_hotkey_text(hotkey_text)

    def _reset_chord(self) -> None:
        """Forget the combination being held.

        Windows swallows the key-up of Alt on Alt+Tab and of the Windows key
        when the Start menu opens, so the set of held modifiers can never be
        trusted to empty itself. Anything that ends the capture - a lost
        focus, a new value from the settings - starts from a clean slate,
        otherwise one lost key-up kills the field until the app restarts.
        """
        self._modifiers_down.clear()
        self._chord_modifiers = []
        self._chord_unnamed_families = []
        self._chord_has_unknown_modifier = False
        self._chord_has_regular_key = False

    def set_hotkey_text(self, text: str) -> None:
        self._reset_chord()
        try:
            self._hotkey = parse(text)
            self.setText(self._hotkey.to_string())
        except HotkeyError:
            self._hotkey = None
            self.setText(text)

    def hotkey_text(self) -> str:
        return self.text().strip()

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Alt+Tab takes the focus and eats the Alt key-up; drop the chord."""
        self._restore_dangling_preview()
        self._reset_chord()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if not self._modifiers_down:
            # Nothing is held, so whatever the last chord left behind is
            # stale - a bare letter typed into the field must not disable the
            # next capture.
            self._reset_chord()
        if key in _MODIFIER_KEYS:
            self._remember_modifier(event)
            self.setText(self._modifier_preview(event) + "…")
            event.accept()
            return

        self._chord_has_regular_key = True
        modifiers = self._modifier_names(event)
        vk = int(event.nativeVirtualKey() or 0)
        if not vk:
            token = _QT_KEY_TOKENS.get(key)
            if token is None and event.text():
                token = event.text().lower()
            vk = key_token_to_vk(token or "") or 0
        if not vk:
            self.captureFailed.emit("Эта клавиша не поддерживается")
            event.accept()
            return

        try:
            hotkey = from_parts(modifiers, vk)
        except HotkeyError as exc:
            self.captureFailed.emit(str(exc))
            event.accept()
            return

        self._hotkey = hotkey
        self.setText(hotkey.to_string())
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() not in _MODIFIER_KEYS:
            event.accept()
            return

        self._modifiers_down.discard(self._modifier_identity(event))
        if self._modifiers_down:
            event.accept()  # other modifiers are still held, wait for them
            return

        committed = self._finish_chord()
        if not committed:
            self._restore_dangling_preview()
        self._reset_chord()
        event.accept()

    def _restore_dangling_preview(self) -> None:
        """A leftover "ctrl+alt+…" would look broken and cannot be saved."""
        if self.text().endswith("…"):
            self.setText(self._hotkey.to_string() if self._hotkey else "")

    def _finish_chord(self) -> bool:
        """Modifiers only, and exactly one of them - that is the hotkey."""
        if self._chord_has_regular_key or self._chord_has_unknown_modifier:
            return False
        named = collapse_altgr(self._chord_modifiers)
        if not self._chord_unnamed_families and len(named) == 1:
            return self._commit_modifier_only(named[0])
        if not named and len(set(self._chord_unnamed_families)) == 1:
            # The user held one modifier and nothing could tell us the side.
            # Saying so beats committing the wrong key silently.
            self.captureFailed.emit(
                side_required_message(self._chord_unnamed_families[0])
            )
            return False
        # Several modifiers and no key: nothing to save, and reverting the
        # preview without a word looks like the field ignored the user.
        self.captureFailed.emit(
            "Как горячая клавиша подходит либо один модификатор, "
            "либо сочетание с обычной клавишей"
        )
        return False

    def _commit_modifier_only(self, name: str) -> bool:
        try:
            hotkey = from_parts([name])
        except HotkeyError as exc:
            self.captureFailed.emit(str(exc))
            return False
        self._hotkey = hotkey
        self.setText(hotkey.to_string())
        return True

    def _remember_modifier(self, event: QKeyEvent) -> None:
        self._modifiers_down.add(self._modifier_identity(event))
        # The side is resolved from values Qt fills in itself; log the raw
        # ones, so a wrong side can be read off the log instead of guessed.
        log.debug(
            "Modifier captured: key=%s vk=%s scancode=%s held=%s",
            int(event.key()),
            int(event.nativeVirtualKey() or 0),
            int(event.nativeScanCode() or 0),
            _held_sides(_QT_KEY_FAMILIES[event.key()])
            if event.key() in _QT_KEY_FAMILIES
            else (),
        )
        family = _QT_KEY_FAMILIES.get(event.key())
        if family is None:
            self._chord_has_unknown_modifier = True  # Caps Lock is no hotkey
            return
        token = self._modifier_side(event)
        if token is None:
            self._chord_unnamed_families.append(family)
        elif token not in self._chord_modifiers:
            self._chord_modifiers.append(token)

    @staticmethod
    def _modifier_identity(event: QKeyEvent) -> int:
        """Tells the two keys of a family apart while both are held."""
        return (
            int(event.nativeScanCode() or 0)
            or int(event.nativeVirtualKey() or 0)
            or int(event.key())
        )

    @staticmethod
    def _modifier_side(event: QKeyEvent) -> str | None:
        """Side-specific name of the modifier just pressed, or None."""
        family = _QT_KEY_FAMILIES[event.key()]
        return resolve_modifier_side(
            int(event.nativeVirtualKey() or 0),
            int(event.nativeScanCode() or 0),
            _held_sides(family),
        )

    @staticmethod
    def _modifier_names(event: QKeyEvent) -> list[str]:
        mods = event.modifiers()
        names: list[str] = []
        if mods & Qt.ControlModifier:
            names.append("ctrl")
        if mods & Qt.AltModifier:
            names.append("alt")
        if mods & Qt.ShiftModifier:
            names.append("shift")
        if mods & Qt.MetaModifier:
            names.append("win")
        return names

    def _modifier_preview(self, event: QKeyEvent) -> str:
        names = self._modifier_names(event)
        key = event.key()
        if key in (Qt.Key_Control,) and "ctrl" not in names:
            names.append("ctrl")
        if key in (Qt.Key_Alt, Qt.Key_AltGr) and "alt" not in names:
            names.append("alt")
        if key == Qt.Key_Shift and "shift" not in names:
            names.append("shift")
        if key == Qt.Key_Meta and "win" not in names:
            names.append("win")
        return "+".join(names) + "+" if names else ""
