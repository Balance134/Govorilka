"""History window: the last dictations, with a copy button on each of them."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..config import history, paths
from ..injection import typer

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

EMPTY_TEXT = "Пока пусто. Продиктуйте что-нибудь — текст появится здесь."


class HistoryWindow(QWidget):
    """Plain on purpose, like the settings window: a list and two buttons.

    Reads the file itself on every ``reload()``, so it can be opened at any
    moment and always shows what is actually stored.
    """

    def __init__(self, icon: QIcon | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Говорилка — история")
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(600, 520)
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("Последние диктовки")
        title.setWordWrap(True)
        layout.addWidget(title)

        privacy = QLabel(
            f"Последние {history.MAX_ENTRIES} диктовок хранятся на этом "
            f"компьютере, в папке {paths.app_data_dir()}. "
            "Кнопка «Очистить историю» стирает их насовсем."
        )
        privacy.setWordWrap(True)
        privacy.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(privacy)

        # Long transcripts must scroll instead of growing the window.
        self._entries_layout = QVBoxLayout()
        self._entries_layout.setAlignment(Qt.AlignTop)
        container = QWidget()
        container.setLayout(self._entries_layout)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(container)
        layout.addWidget(area, 1)

        buttons = QHBoxLayout()
        clear_button = QPushButton("Очистить историю")
        clear_button.clicked.connect(self._on_clear)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.close)
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    # --------------------------------------------------------------- filling
    def reload(self) -> None:
        """Rebuilds the list from the file. Never raises: an empty list is a
        far better outcome here than a window that refuses to open."""
        self._clear_entries()
        try:
            entries = history.load()
        except Exception:
            log.exception("Could not read the history for the window")
            entries = []
        if not entries:
            empty = QLabel(EMPTY_TEXT)
            empty.setWordWrap(True)
            self._entries_layout.addWidget(empty)
            return
        for entry in entries:  # already newest first
            self._entries_layout.addWidget(self._entry_widget(entry))

    def _clear_entries(self) -> None:
        while self._entries_layout.count():
            item = self._entries_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _entry_widget(self, entry: history.HistoryEntry) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        box = QVBoxLayout(frame)

        header = QHBoxLayout()
        caption = QLabel(
            f"{history.format_timestamp(entry.timestamp)} · "
            f"{entry.label()} · {entry.chars} симв."
        )
        copy_button = QPushButton("Копировать")
        copy_button.clicked.connect(lambda _=False, text=entry.text: self._copy(text))
        header.addWidget(caption)
        header.addStretch(1)
        header.addWidget(copy_button)
        box.addLayout(header)

        text = QLabel(entry.text)
        text.setWordWrap(True)
        # Selectable so a part of a long dictation can be picked out by hand.
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(text)
        return frame

    # --------------------------------------------------------------- actions
    def _copy(self, text: str) -> None:
        """The same clipboard path the injection already trusts.

        On Windows that is typer.set_clipboard_text(): it owns a message
        window (Windows refuses the write without one) and normalises the line
        breaks to CR LF, so a multi-line transcript pastes into Notepad as
        paragraphs and not as squares. Qt's clipboard is the fallback for the
        other platforms, where the ctypes path is a no-op.
        """
        try:
            if _IS_WINDOWS:
                typer.set_clipboard_text(text)
            else:
                QApplication.clipboard().setText(text)
        except Exception:
            log.exception("Could not copy a history entry")
            QMessageBox.warning(
                self, "История", "Не удалось скопировать текст в буфер обмена."
            )

    def _on_clear(self) -> None:
        answer = QMessageBox.question(
            self,
            "История",
            "Стереть все сохранённые диктовки? Отменить это будет нельзя.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not history.clear():
            QMessageBox.warning(self, "История", "Не удалось очистить историю.")
        self.reload()
