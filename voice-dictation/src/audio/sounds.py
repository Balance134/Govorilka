"""Two short feedback tones, generated in code - no asset files needed.

Playback happens on a daemon thread so opening the microphone is never
delayed by the beep.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

START_FREQ_HZ = 880
STOP_FREQ_HZ = 520
DURATION_MS = 90

_IS_WINDOWS = sys.platform == "win32"


def _beep(frequency: int, duration_ms: int) -> None:
    if not _IS_WINDOWS:
        return
    try:
        import winsound

        winsound.Beep(frequency, duration_ms)
    except Exception:
        log.debug("Beep failed", exc_info=True)


def _beep_async(frequency: int, duration_ms: int) -> None:
    threading.Thread(
        target=_beep, args=(frequency, duration_ms), daemon=True, name="beep"
    ).start()


def play_start() -> None:
    _beep_async(START_FREQ_HZ, DURATION_MS)


def play_stop() -> None:
    _beep_async(STOP_FREQ_HZ, DURATION_MS)
