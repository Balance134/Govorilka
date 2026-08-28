"""Coordinator: wires hotkey, recorder, Gemini, injection, tray and settings."""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QObject,
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtWidgets import QApplication, QMessageBox

from .audio import sounds
from .audio.recorder import (
    MAX_DURATION_SEC,
    MicrophoneError,
    Recorder,
    is_too_short,
)
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

# A take is force-stopped at this point (the recorder caps its buffer too).
MAX_RECORDING_MS = int(MAX_DURATION_SEC * 1000)

# Longest a single dictation may stay in PROCESSING or TYPING before the
# watchdog frees the hotkey. Deliberately above the worst legal case (upload
# 180 s + file activation 120 s + read 120 s + retry budget) so it only ever
# fires when a worker signal was genuinely lost, never on a slow request.
BUSY_TIMEOUT_MS = 480_000

# How long a worker thread is given to finish while quitting. Past this the
# process exits by itself instead of letting Qt abort on a live QThread.
QUIT_GRACE_MS = 10_000

# ERROR would otherwise be replaced by IDLE within the same event, invisible.
ERROR_VISIBLE_MS = 1_500

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt_x", wintypes.LONG),
            ("pt_y", wintypes.LONG),
        ]
else:  # the filter is never installed anywhere else
    ctypes = None  # type: ignore[assignment]
    _MSG = None  # type: ignore[assignment]


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
        if not self._message_id or _MSG is None:
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message == self._message_id:
                self._callback()
        except Exception:
            log.debug("Native event filter failed", exc_info=True)
        return False, 0


class DictationApp(QObject):
    # Hook callbacks arrive on the hook thread; these signals move them to GUI.
    hotkeyPressed = Signal()
    hotkeyReleased = Signal()
    # Unhandled errors are reported from arbitrary threads; the tray balloon
    # may only be touched on the GUI thread, so they travel through a signal.
    errorReported = Signal(str)

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
        self._tray.iconActivated.connect(self._on_tray_activated)
        self._tray.show()

        if self._config.was_reset:
            self._tray.notify(
                "Файл настроек был повреждён — настройки сброшены, "
                "введите ключ Gemini API заново"
            )

        self._settings = SettingsWindow(self._config, icon)
        self._settings.settingsSaved.connect(self._on_settings_saved)

        self.errorReported.connect(self._tray.notify, Qt.QueuedConnection)
        logging_setup.set_notifier(self.errorReported.emit)

        self.hotkeyPressed.connect(self._start_recording, Qt.QueuedConnection)
        self.hotkeyReleased.connect(self._stop_recording, Qt.QueuedConnection)

        self._recording_timer = self._make_timer(
            MAX_RECORDING_MS, self._on_recording_timeout
        )
        self._busy_timer = self._make_timer(BUSY_TIMEOUT_MS, self._on_busy_timeout)
        self._error_timer = self._make_timer(ERROR_VISIBLE_MS, self._on_error_timeout)

        self._listener: HotkeyListener | None = None
        self._event_filter = _ShowSettingsFilter(
            single_instance.show_settings_message_id(), self.show_settings
        )
        qt_app.installNativeEventFilter(self._event_filter)
        # Logging out of Windows ends the loop without the tray menu: clean up
        # there too, otherwise the hook and the microphone stay behind.
        qt_app.aboutToQuit.connect(self._cleanup)

    def _make_timer(self, interval_ms: int, slot) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval_ms)
        timer.timeout.connect(slot)
        return timer

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

    def _on_tray_activated(self) -> None:
        """Clicking the icon is the cheap way back after a failed hook."""
        if self._shutting_down or self._listener is not None:
            return
        if not self._config.has_api_key():
            return
        log.info("Retrying the hotkey hook after a tray click")
        self._start_listener()

    # ------------------------------------------------------------- settings
    def show_settings(self) -> None:
        self._settings.load_config(self._config)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _on_settings_saved(self, config: AppConfig) -> None:
        try:
            store.save(config)
        except OSError:
            log.exception("Could not write the config file")
            QMessageBox.warning(
                self._settings, "Настройки", "Не удалось сохранить файл настроек."
            )
            return
        self._config = config  # only once the file on disk agrees
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
        # Beep first: opening a WASAPI device takes up to a few hundred
        # milliseconds and the user needs the cue when the key goes down.
        sounds.play_start()
        try:
            self._recorder.start()
        except MicrophoneError as exc:
            self._fail(str(exc))
            return

    def _stop_recording(self) -> None:
        if self._shutting_down:
            return
        if self._state.state != AppState.RECORDING:
            return
        sounds.play_stop()
        try:
            recording = self._recorder.stop()
        except MicrophoneError as exc:
            self._fail(str(exc))
            return
        except Exception:
            log.exception("Recorder failed to stop")
            self._fail("Микрофон недоступен")
            return

        if recording.warning:
            # Partial audio still gets transcribed, but never silently.
            self._tray.notify(recording.warning)

        if is_too_short(recording.wav):
            self._state.force_idle()
            self._tray.notify("Слишком короткая запись")
            return

        if not self._state.to(AppState.PROCESSING):
            return

        vocabulary = build_api_vocabulary(self._config.vocabulary, self._config.replacements)
        worker = TranscribeWorker(
            self._client, recording.wav, vocabulary, list(self._config.language_codes), self
        )
        worker.succeeded.connect(self._on_text_ready)
        worker.failed.connect(self._fail)
        worker.finished.connect(self._on_transcribe_finished)
        worker.finished.connect(worker.deleteLater)
        self._transcribe_worker = worker
        try:
            worker.start()
        except Exception:
            # QThread::start() does not raise on a failed thread, but if it
            # ever does the app must not stay stuck in PROCESSING.
            log.exception("Could not start the transcription thread")
            self._transcribe_worker = None
            self._fail("Не удалось начать распознавание")

    def _on_recording_timeout(self) -> None:
        if self._state.state != AppState.RECORDING:
            return
        log.warning("Recording hit the %.0f s cap", MAX_DURATION_SEC)
        self._recorder.abort()
        self._state.force_idle()
        self._tray.notify(
            "Запись остановлена: превышен предел 5 минут. "
            "Похоже, отпускание клавиши не дошло до приложения."
        )

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
        worker.finished.connect(self._on_inject_finished)
        worker.finished.connect(worker.deleteLater)
        self._inject_worker = worker
        try:
            worker.start()
        except Exception:
            log.exception("Could not start the injection thread")
            self._inject_worker = None
            self._fail("Не удалось вставить текст")

    def _on_injected(self) -> None:
        self._state.force_idle()

    def _on_clipboard_only(self) -> None:
        self._state.force_idle()
        self._tray.notify(
            "Не удалось определить поле ввода. Текст скопирован в буфер обмена"
        )

    # ----------------------------------------------------------- worker ends
    def _on_transcribe_finished(self) -> None:
        # deleteLater destroys the C++ object; keeping the wrapper would make
        # any later isRunning() raise "Internal C++ object already deleted".
        self._transcribe_worker = None

    def _on_inject_finished(self) -> None:
        self._inject_worker = None

    def _on_busy_timeout(self) -> None:
        if not self._state.is_busy() or self._state.state is AppState.RECORDING:
            return
        log.error("Stuck in %s for %d ms, forcing idle", self._state.state, BUSY_TIMEOUT_MS)
        self._state.force_idle()
        self._tray.notify("Обработка не завершилась вовремя. Можно диктовать снова.")

    # ---------------------------------------------------------------- error
    def _fail(self, message: str) -> None:
        self._recorder.abort()
        self._state.to(AppState.ERROR)
        self._tray.notify(message)
        # Leave ERROR on the tooltip for a moment instead of hiding it inside
        # the same event; the hotkey already works again from ERROR.
        self._error_timer.start()

    def _on_error_timeout(self) -> None:
        if self._state.state is AppState.ERROR:
            self._state.force_idle()

    def _on_state_changed(self, state: AppState) -> None:
        self._tray.set_state(state)
        self._recording_timer.stop()
        self._busy_timer.stop()
        if state is AppState.RECORDING:
            self._recording_timer.start()
        elif state in (AppState.PROCESSING, AppState.TYPING):
            self._busy_timer.start()
        if state is not AppState.ERROR:
            self._error_timer.stop()

    # ------------------------------------------------------------- shutdown
    def quit(self) -> None:
        self._cleanup()
        self._qt_app.quit()

    def _cleanup(self) -> None:
        """Idempotent: runs from the tray menu and from aboutToQuit."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down")
        try:
            self._recording_timer.stop()
            self._busy_timer.stop()
            self._error_timer.stop()
            logging_setup.set_notifier(None)
            if self._listener is not None:
                self._listener.stop()
                self._listener = None
            self._recorder.abort()
            self._qt_app.removeNativeEventFilter(self._event_filter)
            try:
                self._tray.settingsRequested.disconnect()
                self._tray.exitRequested.disconnect()
                self._tray.iconActivated.disconnect()
                self._settings.settingsSaved.disconnect()
            except (RuntimeError, TypeError):
                log.debug("Signals were already disconnected", exc_info=True)
            self._settings.close()
            self._settings.deleteLater()
            self._tray.hide()
        except Exception:
            # One broken step must not skip closing the session below.
            log.exception("Cleanup step failed")
        if not self._wait_for_workers():
            self._force_exit()
        self._client.close()  # only now: nobody is using the session anymore

    def _wait_for_workers(self) -> bool:
        """Waits for the network/injection threads. False if one is still alive."""
        finished = True
        for worker in (self._transcribe_worker, self._inject_worker):
            if worker is None:
                continue
            try:
                if not worker.isRunning():
                    continue
                if not worker.wait(QUIT_GRACE_MS):
                    log.warning("%s did not finish in time", type(worker).__name__)
                    finished = False
            except RuntimeError:
                # The C++ object is already gone, so the thread is done.
                log.debug("Worker wrapper outlived its C++ object", exc_info=True)
        return finished

    def _force_exit(self) -> None:
        """A live QThread at interpreter shutdown aborts the process with a
        Windows crash dialog. Leaving on our own terms is nicer."""
        log.warning("Leaving with a worker still running, forcing exit")
        logging.shutdown()
        os._exit(0)
