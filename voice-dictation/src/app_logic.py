"""Pure decision logic behind the Qt slots of the coordinator.

Kept free of Qt so the seams the coordinator relies on - what to do with a
finished take, with a late transcript, with a second hotkey press - can be
tested on any OS.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Iterable, NamedTuple, Optional

from .audio.recorder import CAP_WARNING, MAX_DURATION_SEC
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
LATE_RESULT_NOTICE = (
    "Распознавание завершилось слишком поздно, текст не вставлен. "
    "Продиктуйте ещё раз"
)

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
