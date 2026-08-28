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


def _broken_backups():
    return list(paths.app_data_dir().glob("config.json.broken-*"))


def test_broken_file_is_replaced_not_fatal():
    paths.ensure_dirs()
    paths.config_file().write_text("{not json", encoding="utf-8")
    config = store.load()
    assert config.api_key == ""
    assert config.was_reset
    assert len(_broken_backups()) == 1


def test_second_corruption_keeps_the_first_backup():
    paths.ensure_dirs()
    for text in ("{not json", "{broken again"):
        paths.config_file().write_text(text, encoding="utf-8")
        store.load()
    assert len(_broken_backups()) == 2


def test_healthy_config_is_not_flagged_as_reset():
    store.save(AppConfig(api_key="k"))
    assert not store.load().was_reset


def test_bom_does_not_wipe_the_api_key():
    paths.ensure_dirs()
    paths.config_file().write_text(
        json.dumps({"api_key": "secret"}), encoding="utf-8-sig"
    )
    config = store.load()
    assert config.api_key == "secret"
    assert not config.was_reset
    assert not _broken_backups()


def test_config_is_written_without_a_bom():
    store.save(AppConfig(api_key="k"))
    assert not paths.config_file().read_bytes().startswith(b"\xef\xbb\xbf")


def test_rule_without_variants_is_dropped_everywhere():
    paths.ensure_dirs()
    paths.config_file().write_text(
        json.dumps({"replacements": [{"to": "Vercel", "from": []}, {"to": "n8n", "from": ["н8н"]}]}),
        encoding="utf-8",
    )
    config = store.load()
    assert [rule.to for rule in config.replacements] == ["n8n"]


def test_load_survives_an_unwritable_appdata(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(store, "save", refuse)
    config = store.load()
    assert config.api_key == ""
    assert not paths.config_file().exists()


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
