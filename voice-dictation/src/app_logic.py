"""Pure decision logic behind the Qt slots of the coordinator.

Kept free of Qt so the seams the coordinator relies on - what to do with a
finished take, with a late transcript, with a second hotkey press - can be
tested on any OS.
"""

from __future__ import annotations

import logging
import math
import time
from enum import Enum
from typing import Callable, Iterable, NamedTuple, Optional, Sequence

from .audio.recorder import CAP_WARNING, MAX_DURATION_SEC, MAX_SAMPLE
from .config.model import ReplacementRule
from .config.vocabulary import apply_replacements
from .utils.state import AppState

log = logging.getLogger(__name__)

# The recorder caps its own buffer at MAX_DURATION_SEC and hands the capped
# take back with a warning. The app-level timer is only a backstop for a take
# that stopped being fed, so it must fire LATER than the recorder's own cap -
# otherwise that cap can never be reached in production.
RECORDING_TIMER_MARGIN_MS = 5_000
MAX_RECORDING_MS = int(MAX_DURATION_SEC * 1000) + RECORDING_TIMER_MARGIN_MS

TIMEOUT_NOTICE = (
    "Запись остановлена: превышен предел 5 минут — распознаём то, что успели. "
    "Похоже, отпускание клавиши не дошло до приложения"
)
TOO_SHORT_NOTICE = "Слишком короткая запись"
TOO_SHORT_AFTER_WARNING = "Запись слишком короткая, распознавание не запущено"
LATE_RESULT_BASE = "Распознавание завершилось слишком поздно, текст не вставлен"
# Only when the history could not be written: without it the text is gone and
# dictating again is the only thing left to do.
LATE_RESULT_RETRY = f"{LATE_RESULT_BASE}. Продиктуйте ещё раз"
NOT_INSERTED_BASE = "Текст не вставлен"

BUSY_MESSAGES: dict[AppState, str] = {
    AppState.RECORDING: "Уже идёт запись — отпустите клавишу, чтобы закончить",
    AppState.PROCESSING: "Идёт обработка предыдущей записи",
    AppState.TYPING: "Идёт вставка предыдущей записи",
}
BUSY_FALLBACK = "Идёт обработка предыдущей записи"


def busy_message(state: AppState) -> str:
    """What to tell the user who pressed the hotkey while busy."""
    return BUSY_MESSAGES.get(state, BUSY_FALLBACK)


class StopOutcome(NamedTuple):
    """What to do with a finished take plus the ONE balloon to show for it."""

    transcribe: bool
    notice: Optional[str]


def decide_stop(
    warning: Optional[str], too_short: bool, timed_out: bool = False
) -> StopOutcome:
    """Windows shows one tray balloon at a time, so the reasons are merged
    instead of overwriting one another.

    A take that ran into the time limit is still transcribed: five minutes of
    speech must never be thrown away because a key-up went missing.
    """
    if too_short:
        if warning:
            return StopOutcome(False, f"{warning}. {TOO_SHORT_AFTER_WARNING}")
        return StopOutcome(False, TOO_SHORT_NOTICE)
    if timed_out:
        if warning and warning != CAP_WARNING:  # the cap is what the notice says
            return StopOutcome(True, f"{TIMEOUT_NOTICE}. {warning}")
        return StopOutcome(True, TIMEOUT_NOTICE)
    return StopOutcome(True, warning)


class TextOutcome(Enum):
    """What to do with a transcript the network thread just delivered."""

    INJECT = "inject"
    DROP_SHUTDOWN = "drop_shutdown"
    DROP_LATE = "drop_late"
    EMPTY = "empty"


def decide_text_ready(
    shutting_down: bool, is_current_take: bool, text: str
) -> TextOutcome:
    if shutting_down:
        # Cleanup has already closed the session and hidden the tray; starting
        # an injection now would type into a half-dismantled application.
        return TextOutcome.DROP_SHUTDOWN
    if not is_current_take:
        return TextOutcome.DROP_LATE
    if not text.strip():
        return TextOutcome.EMPTY
    return TextOutcome.INJECT


def capture_target_window(
    get_foreground: Callable[[], int],
    belongs_to_this_process: Callable[[int], bool],
) -> int:
    """Handle of the window the transcript will be typed into, or 0.

    Read while the user is still in their own window, before anything of ours
    is shown. A handle of this process - the recording overlay, the settings
    window - is refused: typing there would swallow the dictation and report
    it as inserted. 0 means "unknown", which sends the text to the clipboard.
    """
    hwnd = get_foreground()
    if not hwnd:
        return 0
    if belongs_to_this_process(hwnd):
        log.warning("Foreground window belongs to this app, not typing into it")
        return 0
    return hwnd


def timings_belong_to(outcome: TextOutcome) -> bool:
    """Whether a delivered transcript may write into the current stopwatch.

    A take the app stopped waiting for still arrives, and its numbers would
    otherwise overwrite those of the dictation running right now.
    """
    return outcome in (TextOutcome.INJECT, TextOutcome.EMPTY)


class TakeGuard:
    """Generation counter telling a live take from an abandoned one.

    The busy watchdog frees the state machine but cannot stop the worker, so
    its result arrives into an IDLE application. Without the counter that
    transcript would be dropped without a word to the user.
    """

    def __init__(self) -> None:
        self._generation = 0

    def begin(self) -> int:
        self._generation += 1
        return self._generation

    def abandon(self) -> None:
        self._generation += 1

    def is_current(self, generation: int) -> bool:
        return generation == self._generation


def safe_apply_replacements(text: str, rules: Iterable[ReplacementRule]) -> str:
    """Belt and braces around the replacement table.

    Losing a finished dictation to a bug in a user-written rule is the worst
    outcome this app has, so a failure falls back to the raw transcript.
    """
    try:
        return apply_replacements(text, rules)
    except Exception:
        log.exception("Replacement table failed, using the raw transcript")
        return text


# ------------------------------------------------------- recording indicator
# Bar heights are fractions of the tallest bar, so the widget owns the pixels
# and this file owns the behaviour.
BAR_COUNT = 6
# A resting bar stays visible as a flat dash: the overlay is a status hint, and
# an empty rectangle would look broken rather than quiet.
BAR_FLOOR = 0.08
# Speech sits far below full scale, so a linear meter barely moves. Everything
# quieter than this counts as silence.
LEVEL_FLOOR_DB = -55.0
# One frame at ~30 FPS. A bar falls from full height to the floor in about a
# third of a second, which reads as a graceful drop instead of flicker.
BAR_DECAY_PER_FRAME = 0.09
# The middle bars are the tall ones; a flat row would look like a progress bar.
BAR_SHAPE = (0.62, 0.86, 1.0, 0.96, 0.78, 0.55)
# Ripple running along the row so the bars do not move as one lump. It only
# ever scales the part above the floor, so silence stays perfectly still.
BAR_RIPPLE = (1.0, 0.92, 0.8, 0.72, 0.8, 0.92)

# The paint loop indexes both tables by bar, so a changed BAR_COUNT must fail
# here at import instead of inside a 30 FPS repaint.
assert len(BAR_SHAPE) == BAR_COUNT, "BAR_SHAPE needs one entry per bar"
assert len(BAR_RIPPLE) == BAR_COUNT, "BAR_RIPPLE needs one entry per bar"

IDLE_BARS: tuple[float, ...] = (BAR_FLOOR,) * BAR_COUNT


def peak_to_level(peak: int) -> float:
    """Loudest sample of a block (0..MAX_SAMPLE) to a 0..1 loudness."""
    if peak <= 0:
        return 0.0
    decibels = 20.0 * math.log10(min(peak, MAX_SAMPLE) / float(MAX_SAMPLE))
    if decibels <= LEVEL_FLOOR_DB:
        return 0.0
    return min(1.0, (decibels - LEVEL_FLOOR_DB) / -LEVEL_FLOOR_DB)


def next_bar_heights(
    previous: Sequence[float], level: float, frame: int = 0
) -> tuple[float, ...]:
    """One frame of the equalizer: rises instantly, falls by a fixed step.

    Pure on purpose - this is the only part of the indicator that can be wrong
    in a way the user would notice.
    """
    level = min(max(level, 0.0), 1.0)
    bars = []
    for index in range(BAR_COUNT):
        ripple = BAR_RIPPLE[(frame + index) % len(BAR_RIPPLE)]
        target = BAR_FLOOR + (1.0 - BAR_FLOOR) * level * BAR_SHAPE[index] * ripple
        was = previous[index] if index < len(previous) else BAR_FLOOR
        height = target if target >= was else max(target, was - BAR_DECAY_PER_FRAME)
        bars.append(min(max(height, BAR_FLOOR), 1.0))
    return tuple(bars)


# -------------------------------------------------------------- microphone
# How long the "speak now" beep waits for the microphone to deliver its first
# block. Opening a WASAPI device costs tens of milliseconds on a built-in
# microphone and far more on a Bluetooth headset; beeping before audio flows
# tells the user to speak into a stream that is not running yet, and the first
# words are lost. A device that never delivers must still get a cue, so the
# wait is capped and the beep goes out anyway.
FIRST_BLOCK_TIMEOUT_SEC = 0.4


class AudioWait(NamedTuple):
    """Outcome of holding the start beep until audio arrives."""

    got_audio: bool
    seconds: float


def wait_for_first_audio(
    wait: Callable[[float], bool],
    timeout: float = FIRST_BLOCK_TIMEOUT_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> AudioWait:
    """Holds the caller until the recorder reports its first block.

    `wait` is Recorder.wait_for_first_block; it returns whether audio arrived
    before the timeout. The measured wait is what the log line reports.
    """
    started = clock()
    got_audio = bool(wait(timeout))
    return AudioWait(got_audio, max(clock() - started, 0.0))


def format_first_audio_wait(waited: AudioWait) -> str:
    """One INFO line per take, so the real open latency can be read off a log
    instead of guessed."""
    if waited.got_audio:
        return "microphone delivered its first block after {:.0f} ms".format(
            waited.seconds * 1000
        )
    return "microphone delivered no audio within {:.0f} ms, beeping anyway".format(
        waited.seconds * 1000
    )


# ------------------------------------------------------------------ timings
def format_timings(
    audio_seconds: float,
    transcribe_seconds: Optional[float],
    inject_seconds: Optional[float],
    chars: int,
) -> str:
    """One line per dictation, so the wait can be measured instead of guessed.

    Never carries the transcript itself - only how many characters it had.
    """
    return "dictation timings: audio={} transcribe={} inject={} chars={}".format(
        _seconds(audio_seconds),
        _seconds(transcribe_seconds),
        _seconds(inject_seconds),
        max(chars, 0),
    )


def _seconds(value: Optional[float]) -> str:
    if value is None:
        return "?"  # the stage never happened, e.g. an injection that failed
    return "{:.1f}s".format(max(value, 0.0))


class DictationTimings:
    """Stopwatch over one dictation, started when the hotkey is released."""

    def __init__(
        self, audio_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._audio_seconds = audio_seconds
        self._released_at = clock()
        self._transcribe_seconds: Optional[float] = None
        self._inject_seconds: Optional[float] = None
        self._chars = 0

    def transcript_ready(self, chars: int) -> None:
        self._transcribe_seconds = self._clock() - self._released_at
        self._chars = chars

    def injected(self) -> None:
        self._inject_seconds = self._clock() - self._released_at - (
            self._transcribe_seconds or 0.0
        )

    def as_line(self) -> str:
        return format_timings(
            self._audio_seconds,
            self._transcribe_seconds,
            self._inject_seconds,
            self._chars,
        )


# ------------------------------------------------------------------ history
HISTORY_HINT = "Текст сохранён в истории"
CLIPBOARD_ONLY_NOTICE = (
    "Не удалось определить поле ввода. Текст скопирован в буфер обмена"
)


def with_history_hint(message: str, saved: bool) -> str:
    """Adds "the text is in the history" to a message about a paste that went
    wrong - but only when the entry really reached the disk, so the app never
    points the user at a history that stayed empty.
    """
    if not saved:
        return message
    text = message.strip().rstrip(".")
    if not text:
        return HISTORY_HINT
    return f"{text}. {HISTORY_HINT}"


LATE_RESULT_NOTICE = f"{LATE_RESULT_BASE}. {HISTORY_HINT}"


def late_result_notice(saved: bool) -> str:
    """A transcript the app decided not to inject is not a lost dictation any
    more: it is on disk, so the user is pointed at the history instead of
    being told to say the whole thing again."""
    if not saved:
        return LATE_RESULT_RETRY
    return with_history_hint(LATE_RESULT_BASE, True)
