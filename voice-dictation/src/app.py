"""Coordinator: wires hotkey, recorder, Gemini, injection, tray and settings."""

from __future__ import annotations

import logging
import os
import sys
import time

from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QObject,
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtWidgets import QApplication, QMessageBox

from .app_logic import (
    CLIPBOARD_ONLY_NOTICE,
    DictationTimings,
    MAX_RECORDING_MS,
    NOT_INSERTED_BASE,
    TakeGuard,
    TextOutcome,
    busy_message,
    capture_target_window,
    decide_stop,
    decide_text_ready,
    late_result_notice,
    safe_apply_replacements,
    timings_belong_to,
    with_history_hint,
)
from .audio import sounds
from .audio.recorder import (
    MAX_DURATION_SEC,
    MicrophoneError,
    Recorder,
    is_too_short,
    wav_duration_seconds,
)
from .config import history, store
from .config.model import AppConfig
from .config.vocabulary import build_api_vocabulary
from .gemini.client import GeminiClient
from .gemini.errors import TranscriptionError
from .hotkey.listener import HotkeyListener
from .hotkey.parser import HotkeyError, parse as parse_hotkey
from .injection import focus, typer
from .tray.tray_icon import TrayIcon, app_icon
from .ui.history_window import HistoryWindow
from .ui.level_indicator import LevelIndicator
from .ui.settings_window import SettingsWindow
from .utils import logging_setup, single_instance
from .utils.state import AppState, StateMachine

log = logging.getLogger(__name__)

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
    # A modifier-only hotkey turned out to be part of an ordinary shortcut.
    hotkeyCancelled = Signal()
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
        self._take = TakeGuard()
        self._timings: DictationTimings | None = None
        # Transcript of the take being injected, waiting to be written to the
        # history once its outcome is known.
        self._pending_text = ""
        self._shutting_down = False

        # The indicator is a comfort, never a requirement: dictation has to
        # work even if the overlay cannot be created at all.
        self._indicator: LevelIndicator | None = None
        try:
            self._indicator = LevelIndicator(self._recorder.peak_level)
        except Exception:
            log.exception("Could not create the recording indicator")

        icon = app_icon()
        self._icon = icon
        self._tray = TrayIcon(self)
        self._tray.settingsRequested.connect(self.show_settings)
        self._tray.historyRequested.connect(self.show_history)
        self._tray.exitRequested.connect(self.quit)
        self._tray.iconActivated.connect(self._on_tray_activated)
        self._tray.show()

        self._settings = SettingsWindow(self._config, icon)
        self._settings.settingsSaved.connect(self._on_settings_saved)
        # Built on the first "История" click, so startup stays as fast as it is.
        self._history_window: HistoryWindow | None = None

        self.errorReported.connect(self._tray.notify, Qt.QueuedConnection)
        logging_setup.set_notifier(self.errorReported.emit)

        self.hotkeyPressed.connect(self._start_recording, Qt.QueuedConnection)
        self.hotkeyReleased.connect(self._stop_recording, Qt.QueuedConnection)
        self.hotkeyCancelled.connect(self._cancel_recording, Qt.QueuedConnection)

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
        # Windows shows one tray balloon at a time, so the two things that can
        # be wrong at startup travel in a single message.
        if not self._config.has_api_key():
            self.show_settings()
            if self._config.was_reset:
                self._notify_when_running(
                    "Файл настроек был повреждён — настройки сброшены. "
                    "Введите ключ Gemini API заново, чтобы начать диктовку"
                )
            else:
                self._notify_when_running(
                    "Введите ключ Gemini API, чтобы начать диктовку"
                )
            return
        if self._config.was_reset:
            self._notify_when_running(
                "Файл настроек был повреждён — настройки сброшены"
            )
        self._start_listener()

    def _notify_when_running(self, message: str) -> None:
        """showMessage before exec() is unreliable; land after the loop is up."""
        QTimer.singleShot(0, lambda: self._notify(message))

    def _notify(self, message: str) -> None:
        if self._shutting_down:
            return
        self._tray.notify(message)

    def _start_listener(self) -> None:
        try:
            hotkey = parse_hotkey(self._config.hotkey)
        except HotkeyError as exc:
            log.error("Bad hotkey in config: %s", exc)
            self._tray.notify(f"Горячая клавиша не задана: {exc}")
            self.show_settings()
            return

        # One listener object for the whole run, even when its hook failed to
        # install: a retry must reuse it instead of adding a second hook.
        if self._listener is None:
            self._listener = HotkeyListener(
                hotkey,
                on_press=self.hotkeyPressed.emit,
                on_release=self.hotkeyReleased.emit,
                on_cancel=self.hotkeyCancelled.emit,
            )
        listener = self._listener
        listener.set_hotkey(hotkey)
        if listener.is_running:
            return
        try:
            listener.start()
        except RuntimeError as exc:
            log.error("Hook unavailable: %s", exc)
            self._tray.notify(str(exc))
            return
        log.info("Hotkey listener started: %s", hotkey.to_string())

    @property
    def _hook_is_up(self) -> bool:
        return self._listener is not None and self._listener.is_running

    def _on_tray_activated(self) -> None:
        """Clicking the icon is the cheap way back after a failed hook."""
        if self._shutting_down:
            return
        if not self._hook_is_up and self._config.has_api_key():
            log.info("Retrying the hotkey hook after a tray click")
            self._start_listener()
            return
        self.show_settings()

    # ------------------------------------------------------------- settings
    def show_settings(self) -> None:
        self._settings.load_config(self._config)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    # -------------------------------------------------------------- history
    def show_history(self) -> None:
        """Opened by an explicit tray click, so taking the focus is correct
        here: a dictation in flight types into the window it remembered when
        the hotkey went down, not into whatever is focused now."""
        if self._shutting_down:
            return
        if self._history_window is None:
            try:
                self._history_window = HistoryWindow(self._icon)
            except Exception:
                log.exception("Could not create the history window")
                self._tray.notify("Не удалось открыть историю")
                return
        window = self._history_window
        window.reload()
        window.show()
        window.raise_()
        window.activateWindow()

    def _record_pending(self, outcome: str) -> bool:
        """Stores the transcript that was handed to the injection worker."""
        text = self._pending_text
        self._pending_text = ""
        return self._record_dictation(text, outcome)

    def _record_dictation(self, text: str, outcome: str) -> bool:
        """Stores one transcript. A failure here is logged and forgotten: the
        dictation itself already happened."""
        if not text:
            return False
        saved = history.add(text, outcome)
        window = self._history_window
        if saved and window is not None and window.isVisible():
            try:
                window.reload()
            except Exception:
                log.exception("Could not refresh the open history window")
        return saved

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
            self._tray.notify(busy_message(self._state.state))
            return
        # Read the target BEFORE the state change: the transition shows the
        # recording overlay, and a window of ours must never become the target.
        target_hwnd = capture_target_window(
            focus.get_foreground_window, focus.belongs_to_this_process
        )
        if not self._state.to(AppState.RECORDING):
            return
        self._target_hwnd = target_hwnd
        # Beep first: opening a WASAPI device takes up to a few hundred
        # milliseconds and the user needs the cue when the key goes down.
        sounds.play_start()
        try:
            self._recorder.start()
        except MicrophoneError as exc:
            self._fail(str(exc))
            return

    def _cancel_recording(self) -> None:
        """The hotkey turned out to be a shortcut's modifier: drop the take."""
        if self._shutting_down:
            return
        if self._state.state != AppState.RECORDING:
            return
        try:
            self._recorder.stop()
        except Exception:
            log.exception("Recorder failed to stop after a cancelled take")
        self._timings = None
        self._state.force_idle()

    def _stop_recording(self, timed_out: bool = False) -> None:
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

        try:
            audio_seconds = wav_duration_seconds(recording.wav)
        except Exception:
            log.debug("Could not measure the take", exc_info=True)
            audio_seconds = 0.0
        self._timings = DictationTimings(audio_seconds)

        # Partial or capped audio still gets transcribed, but never silently.
        outcome = decide_stop(
            recording.warning, is_too_short(recording.wav), timed_out
        )
        if outcome.notice:
            self._tray.notify(outcome.notice)
        if not outcome.transcribe:
            self._log_timings(injected=False)
            self._state.force_idle()
            return

        if not self._state.to(AppState.PROCESSING):
            return

        generation = self._take.begin()
        vocabulary = build_api_vocabulary(self._config.vocabulary, self._config.replacements)
        worker = TranscribeWorker(
            self._client, recording.wav, vocabulary, list(self._config.language_codes), self
        )
        worker.succeeded.connect(
            lambda text, gen=generation: self._on_text_ready(text, gen)
        )
        worker.failed.connect(
            lambda message, gen=generation: self._on_transcribe_failed(message, gen)
        )
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
        # Keep the speech: stop() hands back the take and the flow continues
        # into PROCESSING. A key-up arriving later is a no-op, the state is no
        # longer RECORDING.
        self._stop_recording(timed_out=True)

    # ------------------------------------------------------------ injection
    def _on_text_ready(self, text: str, generation: int) -> None:
        corrected = safe_apply_replacements(text, self._config.replacements)
        outcome = decide_text_ready(
            self._shutting_down, self._take.is_current(generation), corrected
        )
        if self._timings is not None and timings_belong_to(outcome):
            self._timings.transcript_ready(len(corrected))
        if outcome is TextOutcome.DROP_SHUTDOWN:
            # Nothing may be typed or shown at this point, but a plain file
            # write still works - and it is the only way the text survives.
            log.info("Transcript arrived after shutdown, saving it to the history")
            history.add(corrected, history.OUTCOME_SHUTDOWN)
            return
        if outcome is TextOutcome.DROP_LATE:
            # The take was abandoned, but the transcript itself is fine: it
            # goes to the history and the user is sent there to copy it.
            log.warning("Transcript of an abandoned take arrived, saving it")
            saved = self._record_dictation(corrected, history.OUTCOME_LATE)
            self._tray.notify(late_result_notice(saved))
            return
        if outcome is TextOutcome.EMPTY:
            self._fail("Речь не распознана")  # no text at all, nothing to store
            return
        if not self._state.to(AppState.TYPING):
            # ERROR, or an IDLE the watchdog forced while the take still
            # counted as current. Typing is refused, the text is not.
            log.warning("Cannot type from %s, saving the transcript", self._state.state)
            saved = self._record_dictation(corrected, history.OUTCOME_FAILED)
            self._tray.notify(with_history_hint(NOT_INSERTED_BASE, saved))
            return
        self._pending_text = corrected
        worker = InjectWorker(corrected, self._target_hwnd, self._config.paste_mode, self)
        worker.succeeded.connect(self._on_injected)
        worker.clipboardOnly.connect(self._on_clipboard_only)
        worker.failed.connect(self._on_inject_failed)
        worker.finished.connect(self._on_inject_finished)
        worker.finished.connect(worker.deleteLater)
        self._inject_worker = worker
        try:
            worker.start()
        except Exception:
            log.exception("Could not start the injection thread")
            self._inject_worker = None
            self._on_inject_failed("Не удалось вставить текст")

    def _on_transcribe_failed(self, message: str, generation: int) -> None:
        if self._shutting_down:
            return
        if not self._take.is_current(generation):
            # _fail() would abort whatever is being recorded right now.
            log.warning("Failure of an abandoned take arrived, ignoring it")
            return
        self._fail(message)

    def _on_injected(self) -> None:
        # Saved even when the paste looked fine: the user cannot always tell at
        # that moment whether the text landed where they expected.
        self._record_pending(history.OUTCOME_INSERTED)
        if self._shutting_down:
            return
        self._log_timings()
        self._state.force_idle()

    def _on_clipboard_only(self) -> None:
        saved = self._record_pending(history.OUTCOME_CLIPBOARD)
        if self._shutting_down:
            return
        self._log_timings()
        self._state.force_idle()
        self._tray.notify(with_history_hint(CLIPBOARD_ONLY_NOTICE, saved))

    def _on_inject_failed(self, message: str) -> None:
        saved = self._record_pending(history.OUTCOME_FAILED)
        self._fail(with_history_hint(message, saved))

    def _log_timings(self, injected: bool = True) -> None:
        """One line per dictation, failures included: where the wait went.

        A take that failed is exactly the one worth measuring, so the stages it
        never reached are printed as "?" instead of being left out entirely.
        """
        timings = self._timings
        self._timings = None
        if timings is None:
            return
        if injected:
            timings.injected()
        log.info("%s", timings.as_line())

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
        # The worker keeps running; mark its result as belonging to a take
        # nobody waits for anymore.
        self._take.abandon()
        self._state.force_idle()
        self._tray.notify("Обработка не завершилась вовремя. Можно диктовать снова.")

    # ---------------------------------------------------------------- error
    def _fail(self, message: str) -> None:
        if self._shutting_down:
            log.warning("Error after shutdown, not shown: %s", message)
            return
        self._log_timings(injected=False)
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
        self._show_indicator(state is AppState.RECORDING)
        self._recording_timer.stop()
        self._busy_timer.stop()
        if state is AppState.RECORDING:
            self._recording_timer.start()
        elif state in (AppState.PROCESSING, AppState.TYPING):
            self._busy_timer.start()
        if state is not AppState.ERROR:
            self._error_timer.stop()

    # ------------------------------------------------------------ indicator
    def _show_indicator(self, visible: bool) -> None:
        indicator = self._indicator
        if indicator is None:
            return
        try:
            if visible and not self._shutting_down:
                indicator.start()
            else:
                indicator.stop()
        except Exception:
            log.exception("Recording indicator failed, carrying on without it")
            self._indicator = None
            # A failure inside stop() would otherwise strand an always-on-top
            # overlay on the desktop with nothing left holding a reference.
            try:
                indicator.hide()
                indicator.close()
            except Exception:
                log.debug("Could not hide the failed indicator", exc_info=True)

    def _close_indicator(self) -> None:
        indicator = self._indicator
        self._indicator = None
        if indicator is not None:
            indicator.stop()
            # Not deleteLater(): the loop is already past processing deferred
            # deletes by the time cleanup runs.
            indicator.close()

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
        # Each step runs on its own: a deleted C++ object in one of them must
        # not leave the keyboard hook installed or the microphone open.
        for step in (
            self._recording_timer.stop,
            self._busy_timer.stop,
            self._error_timer.stop,
            lambda: logging_setup.set_notifier(None),
            self._stop_listener,
            self._recorder.abort,
            self._close_indicator,
            lambda: self._qt_app.removeNativeEventFilter(self._event_filter),
            self._disconnect_signals,
            self._settings.close,
            self._settings.deleteLater,
            self._close_history_window,
            self._tray.hide,
        ):
            try:
                step()
            except Exception:
                log.exception("Cleanup step failed")
        if not self._wait_for_workers():
            self._force_exit()
        self._client.close()  # only now: nobody is using the session anymore

    def _close_history_window(self) -> None:
        window = self._history_window
        self._history_window = None
        if window is not None:
            window.close()
            window.deleteLater()

    def _stop_listener(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _disconnect_signals(self) -> None:
        try:
            self._tray.settingsRequested.disconnect()
            self._tray.historyRequested.disconnect()
            self._tray.exitRequested.disconnect()
            self._tray.iconActivated.disconnect()
            self._settings.settingsSaved.disconnect()
        except (RuntimeError, TypeError):
            log.debug("Signals were already disconnected", exc_info=True)

    def _wait_for_workers(self) -> bool:
        """Waits for the network/injection threads. False if one is still alive.

        Both share one deadline: the tray icon is already gone, so twice the
        grace period would look like a frozen application.
        """
        finished = True
        deadline = time.monotonic() + QUIT_GRACE_MS / 1000.0
        for worker in (self._transcribe_worker, self._inject_worker):
            if worker is None:
                continue
            try:
                if not worker.isRunning():
                    continue
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0 or not worker.wait(remaining_ms):
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
