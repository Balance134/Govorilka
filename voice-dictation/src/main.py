"""Entry point: single instance guard, logging, Qt application, tray."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # running the script directly, not as a module
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.app import DictationApp  # type: ignore
    from src.config import paths  # type: ignore
    from src.tray.tray_icon import app_icon  # type: ignore
    from src.utils import logging_setup, single_instance  # type: ignore
else:
    from .app import DictationApp
    from .config import paths
    from .tray.tray_icon import app_icon
    from .utils import logging_setup, single_instance

from PySide6.QtWidgets import QApplication, QMessageBox

log = logging.getLogger(__name__)


def main() -> int:
    paths.ensure_dirs()
    logging_setup.setup()
    logging_setup.install_excepthook()

    guard = single_instance.SingleInstance()
    if not guard.acquire():
        # A copy is already running: ask it to show settings and step aside.
        single_instance.signal_existing_instance()
        return 0

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Говорилка")
    qt_app.setQuitOnLastWindowClosed(False)  # the tray keeps the app alive
    qt_app.setWindowIcon(app_icon())

    from PySide6.QtWidgets import QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None, "Говорилка", "Системный трей недоступен — приложение не может работать."
        )
        return 1

    app = DictationApp(qt_app)
    app.start()
    log.info("Application started")
    try:
        return qt_app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
