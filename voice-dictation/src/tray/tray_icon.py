"""Tray icon, tooltip and menu."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..utils.state import STATE_TITLES, AppState

APP_TITLE = "Говорилка"


def assets_dir() -> Path:
    """Works both from source and from a PyInstaller one-file bundle."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


ICON_FILES = {
    AppState.IDLE: "icon_idle.png",
    AppState.RECORDING: "icon_recording.png",
    AppState.PROCESSING: "icon_processing.png",
    AppState.TYPING: "icon_processing.png",
    AppState.ERROR: "icon_idle.png",
}


def load_icon(name: str) -> QIcon:
    path = assets_dir() / name
    return QIcon(str(path))


def app_icon() -> QIcon:
    ico = assets_dir() / "icon.ico"
    if ico.exists():
        return QIcon(str(ico))
    return load_icon("icon_idle.png")


class TrayIcon(QObject):
    settingsRequested = Signal()
    historyRequested = Signal()
    exitRequested = Signal()
    iconActivated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._icons = {state: load_icon(name) for state, name in ICON_FILES.items()}
        self._tray = QSystemTrayIcon(self._icons[AppState.IDLE], parent)
        self._tray.setToolTip(f"{APP_TITLE} — {STATE_TITLES[AppState.IDLE]}")

        menu = QMenu()
        settings_action = QAction("Настройки", menu)
        settings_action.triggered.connect(self.settingsRequested.emit)
        history_action = QAction("История", menu)
        history_action.triggered.connect(self.historyRequested.emit)
        exit_action = QAction("Выход", menu)
        exit_action.triggered.connect(self.exitRequested.emit)
        menu.addAction(settings_action)
        menu.addAction(history_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self._menu = menu  # keep a reference, Qt does not own it
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def set_state(self, state: AppState) -> None:
        self._tray.setIcon(self._icons[state])
        self._tray.setToolTip(f"{APP_TITLE} — {STATE_TITLES[state]}")

    def notify(self, message: str, title: str = APP_TITLE) -> None:
        self._tray.showMessage(title, message, self._icons[AppState.IDLE], 4000)

    def _on_activated(self, reason) -> None:
        # A right-click already opens the context menu and a middle click means
        # nothing here, so only a real activation reaches the coordinator - it
        # decides between retrying the hook and opening the settings.
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.iconActivated.emit()
