"""Application state machine.

Kept free of Qt and WinAPI imports so it can be unit tested on any OS.

Threading contract: GUI thread only. Every caller (hotkey signals, worker
signals, watchdog timers) is already dispatched to the GUI thread, so no lock
is used; calls from another thread are logged loudly instead, because the
`on_change` callback touches the tray icon and would misbehave anyway.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger(__name__)


class AppState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    TYPING = "typing"
    ERROR = "error"


# Allowed transitions. Anything not listed here is rejected, which keeps
# double hotkey presses and late worker callbacks from corrupting the flow.
_TRANSITIONS: dict[AppState, set[AppState]] = {
    AppState.IDLE: {AppState.RECORDING, AppState.ERROR},
    AppState.RECORDING: {AppState.PROCESSING, AppState.IDLE, AppState.ERROR},
    AppState.PROCESSING: {AppState.TYPING, AppState.IDLE, AppState.ERROR},
    AppState.TYPING: {AppState.IDLE, AppState.ERROR},
    AppState.ERROR: {AppState.IDLE, AppState.RECORDING},
}

STATE_TITLES: dict[AppState, str] = {
    AppState.IDLE: "Готов",
    AppState.RECORDING: "Запись",
    AppState.PROCESSING: "Обработка",
    AppState.TYPING: "Обработка",
    AppState.ERROR: "Ошибка",
}

# States during which a new hotkey press must be ignored.
_BUSY_STATES = (AppState.RECORDING, AppState.PROCESSING, AppState.TYPING)


class StateMachine:
    """Tiny guarded state holder owned by the coordinator (GUI thread only)."""

    def __init__(self, on_change: Optional[Callable[[AppState], None]] = None) -> None:
        self._state = AppState.IDLE
        self._on_change = on_change
        self._owner_thread = threading.get_ident()

    @property
    def state(self) -> AppState:
        return self._state

    def can(self, target: AppState) -> bool:
        return target in _TRANSITIONS[self._state]

    def to(self, target: AppState) -> bool:
        """Attempt a transition.

        Returns False when the transition is not allowed AND when the machine
        already is in `target` - callers need to tell "moved" from "was already
        there".
        """
        self._check_thread()
        if target == self._state:
            return False
        if not self.can(target):
            return False
        self._state = target
        if self._on_change is not None:
            self._on_change(target)
        return True

    def force_idle(self) -> None:
        """Used on shutdown, on watchdog timeouts and after an error."""
        self._check_thread()
        if self._state is AppState.IDLE:
            return
        self._state = AppState.IDLE
        if self._on_change is not None:
            self._on_change(AppState.IDLE)

    def is_busy(self) -> bool:
        """Recording counts as busy: a second press must not restart it."""
        return self._state in _BUSY_STATES

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            log.error(
                "StateMachine touched from thread %s, owner is %s",
                threading.get_ident(),
                self._owner_thread,
                stack_info=True,
            )
