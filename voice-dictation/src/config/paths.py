"""Filesystem locations. Config never lives next to the .exe."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "VoiceDictation"


def app_data_dir() -> Path:
    """%APPDATA%\\VoiceDictation, with a fallback for odd environments."""
    roaming = os.environ.get("APPDATA")
    if roaming:
        base = Path(roaming)
    else:
        base = Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME


def config_file() -> Path:
    return app_data_dir() / "config.json"


def history_file() -> Path:
    return app_data_dir() / "history.json"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def log_file() -> Path:
    return logs_dir() / "app.log"


def ensure_dirs() -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
