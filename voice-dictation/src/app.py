"""Coordinator: wires hotkey, recorder, Gemini, injection, tray and settings."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from .audio import sounds
from .audio.recorder import MicrophoneError, Recorder, is_too_short
from .config import store
from .config.model import AppConfig
from .config.vocabulary import apply_replacements, build_api_vocabulary
from .gemini.client import GeminiClient
from .gemini.errors import TranscriptionError
from .hotkey.listener import HotkeyListener
from .hotkey.parser import HotkeyError, parse as parse_hotkey
from .injection import focus, typer
from .tray.tray_icon import TrayIcon, app_icon
from .ui.settings_window import SettingsWindow
from .utils import logging_setup, single_instance
from .utils.state import AppState, StateMachine

log = logging.getLogger(__name__)


class TranscribeWorker(QThread):
    """Runs the network call off the GUI thread."""

    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        client: GeminiClient,
        audio_wav: bytes,
        vocabulary: list[str],
        language_codes: list[str],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._audio = audio_wav
        self._vocabulary = vocabulary
        self._language_codes = language_codes

    def run(self) -> None:
        try:
            text = self._client.transcribe(
                self._audio, self._vocabulary, self._language_codes
            )
        except TranscriptionError as exc:
            if exc.detail:
                log.warning("Transcription failed: %s (%s)", exc.message, exc.detail)
            else:
                log.warning("Transcription failed: %s", exc.message)
            self.failed.emit(exc.message)
            return
        except Exception:
            log.exception("Unexpected transcription failure")
            self.failed.emit("Не удалось распознать речь")
            return
        self.succeeded.emit(text)


class InjectWorker(QThread):
    """Restores focus and types the text; contains blocking sleeps."""

    succeeded = Signal()
    clipboardOnly = Signal()
    failed = Signal(str)

    def __init__(
        self, text: str, target_hwnd: int, paste_mode: str, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._target_hwnd = target_hwnd
        self._paste_mode = paste_mode

    def run(self) -> None:
        try:
            current = focus.get_foreground_window()
            needs_restore = (
                current != self._target_hwnd or focus.belongs_to_this_process(current)
            )
            if needs_restore and not focus.restore_foreground(self._target_hwnd):
                # Never type blindly into whatever happens to be focused.
                try:
                    typer.set_clipboard_text(self._text)
                except typer.InjectionError:
                    log.exception("Clipboard fallback failed")
                    self.failed.emit("Не удалось вставить текст")
                    return
                self.clipboardOnly.emit()
                return
            typer.insert_text(self._text, self._paste_mode)
        except typer.InjectionError as exc:
            log.warning("Injection failed: %s", exc)
            self.failed.emit(str(exc))
            return
        except Exception:
            log.exception("Unexpected injection failure")
            self.failed.emit("Не удалось вставить текст")
            return
        self.succeeded.emit()


class _ShowSettingsFilter(QAbstractNativeEventFilter):
    """Catches the broadcast a second copy sends when it starts."""

    def __init__(self, message_id: int, callback) -> None:
        super().__init__()
        self._message_id = message_id
        self._callback = callback

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt naming
        if not self._message_id or sys.platform != "win32":
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes

            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD),
                    ("pt_x", wintypes.LONG),
                    ("pt_y", wintypes.LONG),
                ]

            msg = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
            if msg.message == self._message_id:
                self._callback()
        except Exception:
            log.debug("Native event filter failed", exc_info=True)
        return False, 0


class DictationApp(QObject):
    # Hook callbacks arrive on the hook thread; these signals move them to GUI.
    hotkeyPressed = Signal()
    hotkeyReleased = Signal()

    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self._qt_app = qt_app
        self._config: AppConfig = store.load()
        self._state = StateMachine(on_change=self._on_state_changed)
        self._recorder = Recorder()
        self._client = GeminiClient(self._config.api_key)
        self._target_hwnd = 0
        self._transcribe_worker: TranscribeWorker | None = None
        self._inject_worker: InjectWorker | None = None
        self._shutting_down = False

        icon = app_icon()
        self._tray = TrayIcon(self)
        self._tray.settingsRequested.connect(self.show_settings)
        self._tray.exitRequested.connect(self.quit)
        self._tray.show()

        self._settings = SettingsWindow(self._config, icon)
        self._settings.settingsSaved.connect(self._on_settings_saved)

        logging_setup.set_notifier(self._tray.notify)

        self.hotkeyPressed.connect(self._start_recording, Qt.QueuedConnection)
        self.hotkeyReleased.connect(self._stop_recording, Qt.QueuedConnection)

        self._listener: HotkeyListener | None = None
        self._event_filter = _ShowSettingsFilter(
            single_instance.show_settings_message_id(), self.show_settings
        )
        qt_app.installNativeEventFilter(self._event_filter)

    # ---------------------------------------------------------------- start
    def start(self) -> None:
        if not self._config.has_api_key():
            self.show_settings()
            self._tray.notify("Введите ключ Gemini API, чтобы начать диктовку")
            return
        self._start_listener()

    def _start_listener(self) -> None:
        try:
            hotkey = parse_hotkey(self._config.hotkey)
        except HotkeyError as exc:
            log.error("Bad hotkey in config: %s", exc)
            self._tray.notify(f"Горячая клавиша не задана: {exc}")
            self.show_settings()
            return

        if self._listener is not None:
            self._listener.set_hotkey(hotkey)
            return

        listener = HotkeyListener(
            hotkey,
            on_press=self.hotkeyPressed.emit,
            on_release=self.hotkeyReleased.emit,
        )
        try:
            listener.start()
        except RuntimeError as exc:
            log.error("Hook unavailable: %s", exc)
            self._tray.notify(str(exc))
            return
        self._listener = listener
        log.info("Hotkey listener started: %s", hotkey.to_string())

    # ------------------------------------------------------------- settings
    def show_settings(self) -> None:
        self._settings.load_config(self._config)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _on_settings_saved(self, config: AppConfig) -> None:
        self._config = config
        try:
            store.save(config)
        except OSError:
            log.exception("Could not write the config file")
            QMessageBox.warning(
                self._settings, "Настройки", "Не удалось сохранить файл настроек."
            )
            return
        self._client.set_api_key(config.api_key)
        self._start_listener()  # re-registers or updates the hook in place
        self._tray.notify("Настройки сохранены. Говорилка работает в трее.")

    # ------------------------------------------------------------ recording
    def _start_recording(self) -> None:
        if self._shutting_down:
            return
        if self._state.is_busy():
            self._tray.notify("Идёт обработка предыдущей записи")
            return
        if not self._state.to(AppState.RECORDING):
            return
        self._target_hwnd = focus.get_foreground_window()
        try:
            self._recorder.start()
        except MicrophoneError as exc:
            self._fail(str(exc))
            return
        sounds.play_start()

    def _stop_recording(self) -> None:
        if self._state.state != AppState.RECORDING:
            return
        sounds.play_stop()
        try:
            audio = self._recorder.stop()
        except MicrophoneError as exc:
            self._fail(str(exc))
            return
        except Exception:
            log.exception("Recorder failed to stop")
            self._fail("Микрофон недоступен")
            return

        if is_too_short(audio):
            self._state.to(AppState.IDLE)
            self._tray.notify("Слишком короткая запись")
            return

        if not self._state.to(AppState.PROCESSING):
            return

        vocabulary = build_api_vocabulary(self._config.vocabulary, self._config.replacements)
        worker = TranscribeWorker(
            self._client, audio, vocabulary, list(self._config.language_codes), self
        )
        worker.succeeded.connect(self._on_text_ready)
        worker.failed.connect(self._fail)
        worker.finished.connect(worker.deleteLater)
        self._transcribe_worker = worker
        worker.start()

    # ------------------------------------------------------------ injection
    def _on_text_ready(self, text: str) -> None:
        corrected = apply_replacements(text, self._config.replacements)
        if not corrected.strip():
            self._fail("Речь не распознана")
            return
        if not self._state.to(AppState.TYPING):
            return
        worker = InjectWorker(corrected, self._target_hwnd, self._config.paste_mode, self)
        worker.succeeded.connect(self._on_injected)
        worker.clipboardOnly.connect(self._on_clipboard_only)
        worker.failed.connect(self._fail)
        worker.finished.connect(worker.deleteLater)
        self._inject_worker = worker
        worker.start()

    def _on_injected(self) -> None:
        self._state.to(AppState.IDLE)

    def _on_clipboard_only(self) -> None:
        self._state.to(AppState.IDLE)
        self._tray.notify(
            "Не удалось определить поле ввода. Текст скопирован в буфер обмена"
        )

    # ---------------------------------------------------------------- error
    def _fail(self, message: str) -> None:
        self._recorder.abort()
        self._state.to(AppState.ERROR)
        self._tray.notify(message)
        self._state.force_idle()

    def _on_state_changed(self, state: AppState) -> None:
        self._tray.set_state(state)

    # ------------------------------------------------------------- shutdown
    def quit(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down")
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._recorder.abort()
        for worker in (self._transcribe_worker, self._inject_worker):
            if worker is not None and worker.isRunning():
                worker.wait(3000)
        self._client.close()
        self._settings.settingsSaved.disconnect()
        self._settings.deleteLater()
        self._tray.hide()
        self._qt_app.quit()
