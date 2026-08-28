"""Read-only field that records a key combination the user presses."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLineEdit

from ..hotkey.parser import Hotkey, HotkeyError, from_parts, key_token_to_vk, parse

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


class HotkeyEdit(QLineEdit):
    """Shows a combination string; typing into it is impossible by design."""

    hotkeyChanged = Signal(str)
    captureFailed = Signal(str)

    def __init__(self, hotkey_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Нажмите нужное сочетание")
        self._hotkey: Hotkey | None = None
        self.set_hotkey_text(hotkey_text)

    def set_hotkey_text(self, text: str) -> None:
        try:
            self._hotkey = parse(text)
            self.setText(self._hotkey.to_string())
        except HotkeyError:
            self._hotkey = None
            self.setText(text)

    def hotkey_text(self) -> str:
        return self.text().strip()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key in _MODIFIER_KEYS:
            self.setText(self._modifier_preview(event) + "…")
            event.accept()
            return

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
        self.hotkeyChanged.emit(hotkey.to_string())
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        # A dangling "ctrl+alt+…" preview would look broken; restore the value.
        if event.key() in _MODIFIER_KEYS and self.text().endswith("…"):
            self.setText(self._hotkey.to_string() if self._hotkey else "")
        event.accept()

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
