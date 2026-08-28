"""Rotating file log plus a global exception hook."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from typing import Callable, Optional

from ..config import paths

MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3

_notifier: Optional[Callable[[str], None]] = None


def setup(level: int = logging.INFO) -> None:
    paths.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.handlers.RotatingFileHandler(
        paths.log_file(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


def set_notifier(notifier: Optional[Callable[[str], None]]) -> None:
    """Where unhandled errors are reported to the user.

    Called from any thread by the excepthooks, so the coordinator registers a
    queued signal here, never a widget method. None clears it on shutdown.
    """
    global _notifier
    _notifier = notifier


def install_excepthook() -> None:
    """Log everything, never let a stray exception kill the app silently."""

    def handle(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("unhandled").error(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        if _notifier is not None:
            try:
                _notifier("Произошла ошибка. Подробности записаны в журнал.")
            except Exception:
                logging.getLogger("unhandled").exception("Notifier failed")

    sys.excepthook = handle

    def handle_thread(args) -> None:
        handle(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = handle_thread
