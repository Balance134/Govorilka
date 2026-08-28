"""Application state machine.

Kept free of Qt and WinAPI imports so it can be unit tested on any OS.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional


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


class StateMachine:
    """Tiny guarded state holder shared by the coordinator and the tray."""

    def __init__(self, on_change: Optional[Callable[[AppState], None]] = None) -> None:
        self._state = AppState.IDLE
        self._on_change = on_change

    @property
    def state(self) -> AppState:
        return self._state

    def can(self, target: AppState) -> bool:
        return target in _TRANSITIONS[self._state]

    def to(self, target: AppState) -> bool:
        """Attempt a transition. Returns False when it is not allowed."""
        if target == self._state:
            return True
        if not self.can(target):
            return False
        self._state = target
        if self._on_change is not None:
            self._on_change(target)
        return True

    def force_idle(self) -> None:
        """Used on shutdown and after an error notification was shown."""
        self._state = AppState.IDLE
        if self._on_change is not None:
            self._on_change(AppState.IDLE)

    def is_busy(self) -> bool:
        return self._state in (AppState.PROCESSING, AppState.TYPING)
