import base64
import importlib
import json
import sys
import types

import pytest

from src.config.model import ReplacementRule
from src.config.vocabulary import build_api_vocabulary
from src.gemini import client as gemini_client


def body(**kwargs):
    return gemini_client.build_request_body(
        kwargs.pop("audio_b64", "QUJD"),
        kwargs.pop("vocabulary", ["n8n"]),
        kwargs.pop("language_codes", []),
        **kwargs,
    )


def test_endpoint_and_model():
    assert gemini_client.INTERACTIONS_URL == (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
    )
    assert body()["model"] == "gemini-3.5-transcribe"
    assert "live" not in gemini_client.MODEL


def test_mode_is_the_string_smart():
    config = body()["generation_config"]["transcription_config"]
    assert config["mode"] == "smart"
    assert isinstance(config["mode"], str)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"audio_b64": "QUJD"},
        {"audio_b64": None, "file_uri": "files/abc"},
    ],
)
def test_smart_mode_incompatible_fields_are_absent(kwargs):
    built = body(**kwargs)
    payload = json.dumps(built)
    assert built["generation_config"]["transcription_config"]["mode"] == "smart"
    assert "timestamp_granularities" not in payload
    assert "diarization_mode" not in payload


def test_inline_audio_shape():
    audio = body(audio_b64="QUJD")["input"][0]
    assert audio == {"type": "audio", "data": "QUJD", "mime_type": "audio/wav"}


def test_file_uri_shape():
    audio = body(audio_b64=None, file_uri="files/abc")["input"][0]
    assert audio == {"type": "audio", "uri": "files/abc", "mime_type": "audio/wav"}


def test_exactly_one_audio_source_required():
    with pytest.raises(ValueError):
        gemini_client.build_request_body(None, [], [])
    with pytest.raises(ValueError):
        gemini_client.build_request_body("QUJD", [], [], file_uri="files/abc")


def test_language_codes_pass_through():
    assert body(language_codes=[])["generation_config"]["transcription_config"][
        "language_codes"
    ] == []
    assert body(language_codes=["ru-RU"])["generation_config"]["transcription_config"][
        "language_codes"
    ] == ["ru-RU"]


def test_vocabulary_carries_terms_from_both_layers():
    rules = [ReplacementRule("Claude Code", ["клоткот"])]
    vocabulary = build_api_vocabulary(["Supabase"], rules)
    sent = body(vocabulary=vocabulary)["generation_config"]["transcription_config"][
        "custom_vocabulary"
    ]
    assert sent == ["Supabase", "Claude Code"]


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"output_text": "привет"}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


def test_transcribe_sends_the_documented_request(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    client = gemini_client.GeminiClient("test-key")
    monkeypatch.setattr(client._session, "post", fake_post)

    text = client.transcribe(b"RIFFdata", ["n8n"], [])

    assert text == "привет"
    assert captured["url"] == gemini_client.INTERACTIONS_URL
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["timeout"] == (10, 120)
    audio = captured["json"]["input"][0]
    assert base64.b64decode(audio["data"]) == b"RIFFdata"


def test_transcribe_without_key_is_an_auth_error():
    from src.gemini.errors import AuthError

    with pytest.raises(AuthError):
        gemini_client.GeminiClient("").transcribe(b"x", [], [])


def test_audio_mime_matches_what_the_recorder_produces(monkeypatch):
    """The recorder writes a plain PCM WAV container - audio/wav, nothing else."""
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    recorder = importlib.import_module("src.audio.recorder")

    wav_bytes = recorder.encode_wav(b"\x00\x01" * 64)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert gemini_client.AUDIO_MIME == "audio/wav"


def test_both_upload_calls_carry_a_timeout(monkeypatch):
    calls = []

    class _Start:
        status_code = 200
        text = ""
        headers = {"x-goog-upload-url": "https://upload.example/session"}

        def json(self):
            return {}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == gemini_client.UPLOAD_URL:
            return _Start()
        if url == "https://upload.example/session":
            return _FakeResponse(payload={"file": {"uri": "files/abc", "name": "files/abc"}})
        return _FakeResponse()

    client = gemini_client.GeminiClient("key")
    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr(client._session, "delete", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr(gemini_client, "INLINE_LIMIT_BYTES", 10)

    assert client.transcribe(b"x" * 100, [], []) == "привет"
    start_kwargs = calls[0][1]
    finish_kwargs = calls[1][1]
    assert start_kwargs["timeout"] == (10, 120)
    assert finish_kwargs["timeout"] == (10, 180)
