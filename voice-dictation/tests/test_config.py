import json

import pytest

from src.config import paths, store
from src.config.model import DEFAULT_HOTKEY, AppConfig, ReplacementRule


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def test_paths_follow_appdata(tmp_path):
    assert paths.app_data_dir() == tmp_path / "VoiceDictation"
    assert paths.config_file() == tmp_path / "VoiceDictation" / "config.json"
    assert paths.log_file() == tmp_path / "VoiceDictation" / "logs" / "app.log"


def test_paths_fall_back_without_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    assert paths.app_data_dir().parts[-3:] == ("AppData", "Roaming", "VoiceDictation")


def test_first_run_creates_config_with_empty_key():
    config = store.load()
    assert config.api_key == ""
    assert config.hotkey == DEFAULT_HOTKEY
    assert paths.config_file().exists()
    assert "Claude Code" in config.vocabulary


def test_round_trip_preserves_everything():
    original = AppConfig(
        api_key="secret",
        vocabulary=["n8n", "Supabase"],
        replacements=[ReplacementRule("n8n", ["эн восемь эн"])],
        hotkey="ctrl+shift+f9",
        language_codes=["ru-RU"],
        paste_mode="sendinput",
    )
    store.save(original)
    loaded = store.load()
    assert loaded.to_dict() == original.to_dict()


def test_migration_ignores_unknown_and_fills_missing():
    paths.ensure_dirs()
    paths.config_file().write_text(
        json.dumps({"api_key": "k", "unknown_field": 42}), encoding="utf-8"
    )
    config = store.load()
    assert config.api_key == "k"
    assert config.hotkey == DEFAULT_HOTKEY
    assert config.paste_mode == "clipboard"
    assert config.language_codes == []
    assert config.vocabulary  # defaults restored


def test_broken_file_is_replaced_not_fatal():
    paths.ensure_dirs()
    paths.config_file().write_text("{not json", encoding="utf-8")
    config = store.load()
    assert config.api_key == ""
    assert paths.config_file().with_suffix(".json.broken").exists()


def test_bad_replacement_entries_are_dropped():
    paths.ensure_dirs()
    paths.config_file().write_text(
        json.dumps(
            {"replacements": [{"to": "n8n", "from": ["н8н"]}, {"nope": 1}, "junk"]}
        ),
        encoding="utf-8",
    )
    config = store.load()
    assert [rule.to for rule in config.replacements] == ["n8n"]


def test_config_is_not_written_next_to_the_executable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.save(AppConfig())
    assert not (tmp_path / "config.json").exists()
    assert paths.config_file().exists()
