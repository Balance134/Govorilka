"""Settings window: API key, both vocabulary layers, hotkey."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config.model import AppConfig, PASTE_MODES
from ..config.vocabulary import (
    ValidationError,
    build_api_vocabulary,
    parse_replacements_text,
    parse_vocabulary_text,
    serialize_replacements,
    serialize_vocabulary,
    validate_replacements,
    validate_vocabulary,
    vocabulary_warnings,
)
from ..hotkey.parser import HotkeyError, parse as parse_hotkey, warnings_for
from .hotkey_edit import HotkeyEdit

log = logging.getLogger(__name__)

PASTE_MODE_LABELS = {
    "clipboard": "Через буфер обмена (быстро, рекомендуется)",
    "sendinput": "Посимвольно (для полей, где вставка запрещена)",
}


class SettingsWindow(QWidget):
    """Emits ``settingsSaved`` with a fresh config; the app applies it live."""

    settingsSaved = Signal(object)

    def __init__(self, config: AppConfig, icon: QIcon | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Говорилка — настройки")
        if icon is not None:
            self.setWindowIcon(icon)
        self.setMinimumWidth(560)
        self._config = config
        self._build_ui()
        self.load_config(config)

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Ключ Gemini API")
        show_button = QPushButton("Показать")
        show_button.setCheckable(True)
        show_button.toggled.connect(self._toggle_key_visibility)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit)
        key_row.addWidget(show_button)
        key_container = QWidget()
        key_container.setLayout(key_row)
        form.addRow("Ключ Gemini API", key_container)

        self.vocabulary_edit = QPlainTextEdit()
        self.vocabulary_edit.setPlaceholderText("n8n, Supabase, pgvector")
        self.vocabulary_edit.setFixedHeight(80)
        form.addRow("Словарь терминов", self.vocabulary_edit)
        vocabulary_hint = QLabel(
            "Термины через запятую. Подсказывает модели правильные написания."
        )
        vocabulary_hint.setWordWrap(True)
        form.addRow("", vocabulary_hint)

        self.replacements_edit = QPlainTextEdit()
        self.replacements_edit.setPlaceholderText("Claude Code = клод код, клоткот")
        self.replacements_edit.setFixedHeight(140)
        form.addRow("Правила замен", self.replacements_edit)
        replacements_hint = QLabel(
            "Исправляет то, что модель услышала неправильно. "
            "Слева — как должно быть, справа — как слышится. "
            "Первый знак «=» в строке делит её пополам, поэтому в левой части "
            "знака «=» быть не должно."
        )
        replacements_hint.setWordWrap(True)
        form.addRow("", replacements_hint)

        self.hotkey_edit = HotkeyEdit(self._config.hotkey)
        self.hotkey_edit.captureFailed.connect(self._on_capture_failed)
        form.addRow("Горячая клавиша", self.hotkey_edit)
        hotkey_hint = QLabel("Нажмите поле и наберите сочетание. Диктовка идёт, пока клавиши удерживаются.")
        hotkey_hint.setWordWrap(True)
        form.addRow("", hotkey_hint)

        self.paste_mode_box = QComboBox()
        for mode in PASTE_MODES:
            self.paste_mode_box.addItem(PASTE_MODE_LABELS[mode], mode)
        form.addRow("Способ вставки", self.paste_mode_box)

        self.autodetect_box = QCheckBox("Определять язык автоматически")
        self.autodetect_box.setChecked(True)
        self.autodetect_box.toggled.connect(self._toggle_languages)
        form.addRow("", self.autodetect_box)

        self.languages_edit = QLineEdit()
        self.languages_edit.setPlaceholderText("ru-RU, en-US")
        form.addRow("Языки", self.languages_edit)

        layout.addLayout(form)

        self.save_button = QPushButton("Сохранить и запустить")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._on_save)
        layout.addWidget(self.save_button)

    def _toggle_key_visibility(self, shown: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)

    def _toggle_languages(self, auto: bool) -> None:
        self.languages_edit.setEnabled(not auto)
        if auto:
            self.languages_edit.clear()

    def _on_capture_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Горячая клавиша", message)

    # -------------------------------------------------------------- config
    def load_config(self, config: AppConfig) -> None:
        self._config = config
        self.api_key_edit.setText(config.api_key)
        self.vocabulary_edit.setPlainText(serialize_vocabulary(config.vocabulary))
        self.replacements_edit.setPlainText(serialize_replacements(config.replacements))
        self.hotkey_edit.set_hotkey_text(config.hotkey)
        index = self.paste_mode_box.findData(config.paste_mode)
        self.paste_mode_box.setCurrentIndex(index if index >= 0 else 0)
        auto = not config.language_codes
        self.autodetect_box.setChecked(auto)
        self.languages_edit.setText(", ".join(config.language_codes))
        self.languages_edit.setEnabled(not auto)

    def _collect(self) -> AppConfig:
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            raise ValidationError("Введите ключ Gemini API")

        vocabulary = parse_vocabulary_text(self.vocabulary_edit.toPlainText())
        replacements = validate_replacements(
            parse_replacements_text(self.replacements_edit.toPlainText())
        )
        # Both layers land in one custom_vocabulary, so the limit is on the sum.
        validate_vocabulary(build_api_vocabulary(vocabulary, replacements))

        hotkey_text = self.hotkey_edit.hotkey_text()
        hotkey = parse_hotkey(hotkey_text)  # raises HotkeyError with a message

        if self.autodetect_box.isChecked():
            language_codes: list[str] = []
        else:
            language_codes = [
                code.strip()
                for code in self.languages_edit.text().split(",")
                if code.strip()
            ]

        return AppConfig(
            api_key=api_key,
            vocabulary=vocabulary,
            replacements=replacements,
            hotkey=hotkey.to_string(),
            language_codes=language_codes,
            paste_mode=self.paste_mode_box.currentData() or "clipboard",
        )

    def _on_save(self) -> None:
        try:
            config = self._collect()
        except (ValidationError, HotkeyError) as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            return
        except Exception:
            log.exception("Unexpected failure while collecting settings")
            QMessageBox.warning(
                self, "Проверьте настройки", "Не удалось сохранить настройки."
            )
            return

        warnings = vocabulary_warnings(
            build_api_vocabulary(config.vocabulary, config.replacements)
        )
        try:
            warnings += warnings_for(parse_hotkey(config.hotkey))
        except HotkeyError:
            pass
        if warnings:
            QMessageBox.information(self, "Предупреждение", "\n\n".join(warnings))

        self._config = config
        self.settingsSaved.emit(config)
        self.hide()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Closing the window must not quit the app - it lives in the tray.
        event.ignore()
        self.hide()
