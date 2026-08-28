"""Reading, writing and soft-migrating config.json."""

from __future__ import annotations

import json
import logging
from typing import Any

from . import paths
from .model import (
    DEFAULT_HOTKEY,
    DEFAULT_PASTE_MODE,
    DEFAULT_VOCABULARY,
    PASTE_MODES,
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

    language_codes = raw.get("language_codes")
    if isinstance(language_codes, list):
        config.language_codes = [item for item in language_codes if isinstance(item, str)]
    else:
        config.language_codes = []

    paste_mode = raw.get("paste_mode")
    config.paste_mode = paste_mode if paste_mode in PASTE_MODES else DEFAULT_PASTE_MODE

    return config


def load() -> AppConfig:
    """Never raises: a broken file is backed up and replaced by defaults."""
    path = paths.config_file()
    if not path.exists():
        config = AppConfig()
        config.api_key = ""
        save(config)
        return config
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("Config unreadable, falling back to defaults")
        try:
            path.replace(path.with_suffix(".json.broken"))
        except OSError:
            log.exception("Could not preserve the broken config")
        config = AppConfig()
        save(config)
        return config
    return from_dict(raw)


def save(config: AppConfig) -> None:
    paths.ensure_dirs()
    path = paths.config_file()
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)  # atomic on Windows and POSIX alike
