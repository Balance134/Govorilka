"""Recording overlay: a small equalizer near the bottom of the screen.

Shown only while the microphone is open, and driven by the real signal - the
bars stand still when nothing is said. The window must never take focus: this
application types into whatever the user was working in, so stealing focus
would break dictation itself.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from ..app_logic import BAR_COUNT, IDLE_BARS, next_bar_heights, peak_to_level

log = logging.getLogger(__name__)

FRAME_INTERVAL_MS = 33  # ~30 FPS

# Logical pixels: Qt scales them for the display the window lands on.
WIDTH = 180
HEIGHT = 48
# Distance from the bottom of the work area, as a share of its height. Enough
# to clear a taskbar that Windows placed inside the work area anyway.
BOTTOM_MARGIN_RATIO = 0.12

PADDING = 10
BAR_WIDTH = 10
BAR_GAP = 8
BAR_RADIUS = 5
BACKGROUND_RADIUS = 12

BACKGROUND_COLOR = QColor(24, 24, 28, 200)
BAR_COLOR = QColor(120, 190, 255, 235)


class LevelIndicator(QWidget):
    """Frameless click-through overlay fed by a peak-level callable."""

    def __init__(self, peak_provider, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.resize(WIDTH, HEIGHT)

        self._peak_provider = peak_provider
        self._bars = IDLE_BARS
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Places the overlay on the screen in use and starts the animation."""
        self._bars = IDLE_BARS
        self._frame = 0
        self._move_into_place()
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self) -> None:
        # Nothing polls the recorder while idle: the timer goes down with the
        # window, not alongside it.
        self._timer.stop()
        self.hide()
        self._bars = IDLE_BARS

    def _move_into_place(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:  # no display at all, e.g. a session being torn down
            return
        area = screen.availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + area.height() - int(area.height() * BOTTOM_MARGIN_RATIO)
        self.move(x, max(area.y(), y - self.height()))

    # ------------------------------------------------------------ animation
    def _tick(self) -> None:
        try:
            peak = self._peak_provider()
        except Exception:
            log.debug("Level provider failed", exc_info=True)
            peak = 0
        self._frame += 1
        self._bars = next_bar_heights(self._bars, peak_to_level(peak), self._frame)
        self.update()

    # -------------------------------------------------------------- drawing
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        painter.setBrush(BACKGROUND_COLOR)
        painter.drawRoundedRect(
            QRectF(0, 0, self.width(), self.height()),
            BACKGROUND_RADIUS,
            BACKGROUND_RADIUS,
        )

        painter.setBrush(BAR_COLOR)
        row_width = BAR_COUNT * BAR_WIDTH + (BAR_COUNT - 1) * BAR_GAP
        left = (self.width() - row_width) / 2.0
        available = self.height() - 2 * PADDING
        middle = self.height() / 2.0
        for index, share in enumerate(self._bars):
            height = max(2.0, available * share)
            radius = min(BAR_RADIUS, height / 2.0)
            bar = QRectF(
                left + index * (BAR_WIDTH + BAR_GAP),
                middle - height / 2.0,  # bars grow from the middle, both ways
                BAR_WIDTH,
                height,
            )
            painter.drawRoundedRect(bar, radius, radius)
