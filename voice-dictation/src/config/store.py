"""Reading, writing and soft-migrating config.json."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .model import (
    DEFAULT_HOTKEY,
    DEFAULT_LANGUAGE_CODES,
    DEFAULT_PASTE_MODE,
    DEFAULT_TRANSCRIPTION_MODE,
    DEFAULT_VOCABULARY,
    PASTE_MODES,
    TRANSCRIPTION_MODES,
    AppConfig,
    ReplacementRule,
)
from .vocabulary import dedupe_terms

log = logging.getLogger(__name__)


def _default_replacements() -> list[ReplacementRule]:
    return AppConfig().replacements


def from_dict(raw: Any) -> AppConfig:
    """Unknown keys are ignored, missing keys fall back to defaults."""
    config = AppConfig()
    if not isinstance(raw, dict):
        return config

    api_key = raw.get("api_key")
    if isinstance(api_key, str):
        config.api_key = api_key.strip()

    vocabulary = raw.get("vocabulary")
    if isinstance(vocabulary, list):
        config.vocabulary = dedupe_terms(
            [item for item in vocabulary if isinstance(item, str)]
        )
    else:
        config.vocabulary = list(DEFAULT_VOCABULARY)

    replacements = raw.get("replacements")
    if isinstance(replacements, list):
        parsed = [ReplacementRule.from_dict(item) for item in replacements]
        config.replacements = [rule for rule in parsed if rule is not None]
    else:
        config.replacements = _default_replacements()

    hotkey = raw.get("hotkey")
    config.hotkey = hotkey.strip() if isinstance(hotkey, str) and hotkey.strip() else DEFAULT_HOTKEY

    # A list that is already there wins, even when empty: an empty list is the
    # user's own "detect the language automatically" and must survive the
    # arrival of the Russian default. Only a config without the key at all -
    # a fresh one, or one hand-edited down - gets the new default.
    language_codes = raw.get("language_codes")
    if isinstance(language_codes, list):
        config.language_codes = [item for item in language_codes if isinstance(item, str)]
    else:
        config.language_codes = list(DEFAULT_LANGUAGE_CODES)

    transcription_mode = raw.get("transcription_mode")
    config.transcription_mode = (
        transcription_mode
        if transcription_mode in TRANSCRIPTION_MODES
        else DEFAULT_TRANSCRIPTION_MODE
    )

    paste_mode = raw.get("paste_mode")
    config.paste_mode = paste_mode if paste_mode in PASTE_MODES else DEFAULT_PASTE_MODE

    return config


def load() -> AppConfig:
    """Never raises: a broken file is backed up and replaced by defaults.

    ``was_reset`` on the returned config tells the caller that settings were
    lost, so it can say so instead of silently starting from scratch.
    """
    path = paths.config_file()
    if not path.exists():
        config = AppConfig()
        config.api_key = ""
        _save_quietly(config)
        return config
    try:
        # utf-8-sig: Notepad's "UTF-8 with BOM" must not look like a broken file.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.exception("Config unreadable, falling back to defaults")
        try:
            # Timestamped: a second corruption must not destroy the first backup.
            path.replace(_backup_path(path))
        except OSError:
            log.exception("Could not preserve the broken config")
        config = AppConfig()
        config.was_reset = True
        _save_quietly(config)
        return config
    return from_dict(raw)


def _backup_path(path: Path) -> Path:
    """config.json.broken-<timestamp>, with a counter for the same second."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.broken-{stamp}")
    suffix = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.broken-{stamp}-{suffix}")
        suffix += 1
    return candidate


def _save_quietly(config: AppConfig) -> None:
    """A read-only %APPDATA% must not crash startup: defaults stay in memory."""
    try:
        save(config)
    except OSError:
        log.exception("Could not write the config file")


def save(config: AppConfig) -> None:
    paths.ensure_dirs()
    path = paths.config_file()
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())  # a power loss must not leave an empty config
    tmp.replace(path)  # atomic on Windows and POSIX alike
