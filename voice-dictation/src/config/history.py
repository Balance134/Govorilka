"""The last dictations, kept on disk next to the config.

A safety net: every finished transcript is written here together with what
happened to it, so a paste that landed in the wrong window can still be copied
by hand. Nothing in this module may raise - a dictation must never be lost
because its own safety net failed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import paths

log = logging.getLogger(__name__)

# Ten, not the three the user asked for: they cost nothing and cover the case
# of noticing two dictations later that one of them went missing.
MAX_ENTRIES = 10

OUTCOME_INSERTED = "inserted"
OUTCOME_CLIPBOARD = "clipboard"
OUTCOME_FAILED = "failed"
# A finished, correct transcript the app deliberately did not inject: the take
# was abandoned by the watchdog, or the app was already closing. Exactly the
# case the history exists for - the text is fine, it just needs copying.
OUTCOME_LATE = "late"
OUTCOME_SHUTDOWN = "shutdown"

OUTCOME_LABELS = {
    OUTCOME_INSERTED: "Вставлено",
    OUTCOME_CLIPBOARD: "Скопировано в буфер обмена",
    OUTCOME_FAILED: "Вставить не удалось",
    OUTCOME_LATE: "Не вставлен — распознан слишком поздно",
    OUTCOME_SHUTDOWN: "Не вставлен — приложение закрывалось",
}
UNKNOWN_OUTCOME_LABEL = "Неизвестно"


@dataclass
class HistoryEntry:
    """One dictation. Only the text, when it happened and how it ended."""

    text: str
    timestamp: str  # ISO 8601, local time
    outcome: str
    chars: int

    @classmethod
    def create(cls, text: str, outcome: str) -> "HistoryEntry":
        return cls(
            text=text,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            outcome=outcome,
            chars=len(text),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "timestamp": self.timestamp,
            "outcome": self.outcome,
            "chars": self.chars,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "HistoryEntry | None":
        """None for anything that is not a usable entry; the rest survives."""
        if not isinstance(raw, dict):
            return None
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            return None
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, str):
            timestamp = ""
        outcome = raw.get("outcome")
        if outcome not in OUTCOME_LABELS:
            outcome = OUTCOME_FAILED
        chars = raw.get("chars")
        if not isinstance(chars, int) or isinstance(chars, bool) or chars < 0:
            chars = len(text)
        return cls(text=text, timestamp=timestamp, outcome=outcome, chars=chars)

    def label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, UNKNOWN_OUTCOME_LABEL)


def format_timestamp(timestamp: str) -> str:
    """ISO stamp to the short form shown in the window: ``28.08 14:05``."""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return timestamp or "—"
    return moment.strftime("%d.%m %H:%M")


def load() -> list[HistoryEntry]:
    """Never raises: a broken or unreadable file reads as an empty history."""
    path = paths.history_file()
    try:
        if not path.exists():
            return []
        # utf-8-sig for the same reason as the config: a BOM left by an editor
        # must not look like corruption.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.exception("History unreadable, starting from an empty one")
        return []
    except Exception:  # a surprise here still must not reach the dictation
        log.exception("History could not be read")
        return []
    return _entries_from(raw)


def _entries_from(raw: Any) -> list[HistoryEntry]:
    if isinstance(raw, dict):
        items = raw.get("entries")
    else:
        items = raw  # tolerate a bare list, older shape or a hand edit
    if not isinstance(items, list):
        return []
    parsed = [HistoryEntry.from_dict(item) for item in items]
    return [entry for entry in parsed if entry is not None][:MAX_ENTRIES]


def add(text: str, outcome: str) -> bool:
    """Puts one dictation at the top of the list. False if it could not be
    stored - the caller carries on regardless."""
    if not text:
        return False
    try:
        entries = [HistoryEntry.create(text, outcome), *load()][:MAX_ENTRIES]
        _write(entries)
    except Exception:
        log.exception("Could not write the dictation history")
        return False
    return True


def clear() -> bool:
    """Empties the file itself, not just the window. False on failure."""
    try:
        _write([])
    except Exception:
        log.exception("Could not clear the dictation history")
        return False
    return True


def _write(entries: list[HistoryEntry]) -> None:
    paths.ensure_dirs()
    path = paths.history_file()
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(
        {"entries": [entry.to_dict() for entry in entries]},
        ensure_ascii=False,
        indent=2,
    )
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())  # a power loss must not leave half a line
    tmp.replace(path)  # atomic on Windows and POSIX alike
