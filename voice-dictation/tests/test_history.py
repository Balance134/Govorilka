"""Dictation history: trimming, corruption, unwritable disks, round trips."""

import json

import pytest

from src.config import history, paths


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def write_raw(payload: str, encoding: str = "utf-8") -> None:
    paths.ensure_dirs()
    paths.history_file().write_text(payload, encoding=encoding)


def texts() -> list[str]:
    return [entry.text for entry in history.load()]


def test_path_lives_in_appdata(tmp_path):
    assert paths.history_file() == tmp_path / "VoiceDictation" / "history.json"


def test_empty_history_when_nothing_was_written():
    assert history.load() == []


def test_add_keeps_newest_first_and_trims_to_ten():
    for index in range(15):
        assert history.add(f"фраза {index}", history.OUTCOME_INSERTED)
    stored = texts()
    assert len(stored) == history.MAX_ENTRIES == 10
    assert stored[0] == "фраза 14"
    assert stored[-1] == "фраза 5"


def test_empty_text_is_not_stored():
    assert history.add("", history.OUTCOME_INSERTED) is False
    assert history.load() == []


def test_outcome_survives_a_round_trip():
    history.add("вставилось", history.OUTCOME_INSERTED)
    history.add("только буфер", history.OUTCOME_CLIPBOARD)
    history.add("не вставилось", history.OUTCOME_FAILED)
    history.add("слишком поздно", history.OUTCOME_LATE)
    history.add("на выходе", history.OUTCOME_SHUTDOWN)
    entries = history.load()
    assert [entry.outcome for entry in entries] == [
        history.OUTCOME_SHUTDOWN,
        history.OUTCOME_LATE,
        history.OUTCOME_FAILED,
        history.OUTCOME_CLIPBOARD,
        history.OUTCOME_INSERTED,
    ]
    assert entries[2].label() == "Вставить не удалось"


def test_a_late_transcript_is_kept_instead_of_being_lost():
    """The case the history exists for: a finished, correct transcript the app
    refused to inject because the take had already been abandoned."""
    assert history.add("длинная фраза, которую не хочется диктовать заново",
                       history.OUTCOME_LATE) is True
    entry = history.load()[0]
    assert entry.text == "длинная фраза, которую не хочется диктовать заново"
    assert entry.outcome == history.OUTCOME_LATE
    assert entry.label() == "Не вставлен — распознан слишком поздно"


def test_a_transcript_that_arrived_during_shutdown_is_kept():
    assert history.add("пришло на выходе", history.OUTCOME_SHUTDOWN) is True
    entry = history.load()[0]
    assert entry.outcome == history.OUTCOME_SHUTDOWN
    assert entry.label() == "Не вставлен — приложение закрывалось"


@pytest.mark.parametrize(
    "text",
    [
        "Привет, это проверка — кириллица и тире",
        "первая строка\nвторая строка\r\nтретья",
        'кавычки "двойные" и «ёлочки», обратный слэш \\ и \\"',
        "путь C:\\Users\\Balance\\Documents",
    ],
)
def test_text_survives_a_round_trip(text):
    history.add(text, history.OUTCOME_INSERTED)
    entry = history.load()[0]
    assert entry.text == text
    assert entry.chars == len(text)


def test_timestamp_is_iso_and_formats_short():
    history.add("что-то", history.OUTCOME_INSERTED)
    entry = history.load()[0]
    assert "T" in entry.timestamp
    formatted = history.format_timestamp(entry.timestamp)
    assert len(formatted) == len("28.08 14:05")


def test_format_timestamp_survives_garbage():
    assert history.format_timestamp("не дата") == "не дата"
    assert history.format_timestamp("") == "—"


def test_broken_json_reads_as_empty():
    write_raw('{"entries": [{"text": "начало"')
    assert history.load() == []


def test_truncated_file_reads_as_empty():
    history.add("фраза", history.OUTCOME_INSERTED)
    path = paths.history_file()
    path.write_text(path.read_text(encoding="utf-8")[:20], encoding="utf-8")
    assert history.load() == []


def test_bom_prefixed_file_is_read_normally():
    write_raw(
        json.dumps(
            {
                "entries": [
                    {
                        "text": "с меткой BOM",
                        "timestamp": "2026-08-28T14:05:00",
                        "outcome": history.OUTCOME_CLIPBOARD,
                        "chars": 12,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )
    entry = history.load()[0]
    assert entry.text == "с меткой BOM"
    assert entry.outcome == history.OUTCOME_CLIPBOARD


def test_unexpected_shapes_read_as_empty():
    write_raw('"просто строка"')
    assert history.load() == []
    write_raw('{"entries": {"text": "не список"}}')
    assert history.load() == []


def test_bare_list_and_broken_items_are_tolerated():
    write_raw(
        json.dumps(
            [
                {"text": "уцелела"},
                {"no_text": 1},
                "мусор",
                {"text": "", "outcome": history.OUTCOME_INSERTED},
            ],
            ensure_ascii=False,
        )
    )
    entries = history.load()
    assert [entry.text for entry in entries] == ["уцелела"]
    assert entries[0].chars == len("уцелела")
    assert entries[0].outcome == history.OUTCOME_FAILED  # unknown means "unsure"


def test_a_hand_grown_file_is_trimmed_on_read():
    write_raw(
        json.dumps(
            {"entries": [{"text": f"строка {i}"} for i in range(25)]},
            ensure_ascii=False,
        )
    )
    assert len(history.load()) == history.MAX_ENTRIES


def test_unwritable_location_loses_the_entry_without_raising(tmp_path):
    # A plain file where the folder should be: every write below it fails, the
    # way a locked or full disk does in production.
    (tmp_path / "VoiceDictation").write_text("not a directory", encoding="utf-8")
    assert history.add("пропадёт", history.OUTCOME_INSERTED) is False
    assert history.load() == []
    assert history.clear() is False


def test_clear_empties_the_file():
    history.add("первая", history.OUTCOME_INSERTED)
    history.add("вторая", history.OUTCOME_FAILED)
    assert history.clear() is True
    assert history.load() == []
    stored = json.loads(paths.history_file().read_text(encoding="utf-8"))
    assert stored == {"entries": []}


def test_write_goes_through_a_temp_file_and_lands_on_history_json(monkeypatch):
    from pathlib import Path

    original = Path.replace
    seen = []

    def spy(self, target):
        seen.append((self.name, Path(target).name, self.exists()))
        return original(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    history.add("через временный файл", history.OUTCOME_INSERTED)

    assert seen == [("history.json.tmp", "history.json", True)]
    assert paths.history_file().exists()
    assert not (paths.app_data_dir() / "history.json.tmp").exists()
