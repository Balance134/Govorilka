import pytest
import requests

from src.gemini import client as gemini_client
from src.gemini.errors import (
    AudioFormatError,
    AuthError,
    BadRequestError,
    EmptyTranscriptError,
    MalformedResponseError,
    NetworkError,
    RateLimitError,
    ServiceUnavailableError,
)


def test_output_text_at_the_top_level():
    assert gemini_client.extract_text({"output_text": "привет мир"}) == "привет мир"


def test_output_text_nested_in_interaction():
    payload = {"interaction": {"id": "x", "output_text": "текст"}}
    assert gemini_client.extract_text(payload) == "текст"


def test_steps_content_shape():
    payload = {
        "id": "abc",
        "steps": [
            {"content": [{"type": "text", "text": "первая часть"}]},
            {"content": [{"type": "text", "text": "вторая часть"}]},
        ],
    }
    assert gemini_client.extract_text(payload) == "первая часть вторая часть"


def test_output_list_shape():
    payload = {"output": [{"type": "text", "text": "распознанный текст"}]}
    assert gemini_client.extract_text(payload) == "распознанный текст"


def test_empty_but_well_formed_response():
    with pytest.raises(EmptyTranscriptError):
        gemini_client.extract_text({"id": "abc", "steps": [{"content": []}]})
    with pytest.raises(EmptyTranscriptError):
        gemini_client.extract_text({"output_text": "   "})


def test_unrecognisable_response():
    with pytest.raises(MalformedResponseError):
        gemini_client.extract_text({"totally": {"different": 1}})
    with pytest.raises(MalformedResponseError):
        gemini_client.extract_text("строка вместо объекта")


class _Resp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _client_returning(monkeypatch, response):
    client = gemini_client.GeminiClient("key")
    monkeypatch.setattr(client._session, "post", lambda *a, **k: response)
    return client


@pytest.mark.parametrize(
    "status,text,expected",
    [
        (401, "bad key", AuthError),
        (403, "forbidden", AuthError),
        (429, "quota", RateLimitError),
        (400, "invalid audio mime type", AudioFormatError),
        (400, "something else", BadRequestError),
        (500, "boom", ServiceUnavailableError),
        (503, "unavailable", ServiceUnavailableError),
    ],
)
def test_http_errors_map_to_typed_exceptions(monkeypatch, status, text, expected):
    client = _client_returning(monkeypatch, _Resp(status, text))
    with pytest.raises(expected):
        client.transcribe(b"audio", [], [])


def test_non_json_body_is_malformed(monkeypatch):
    client = _client_returning(monkeypatch, _Resp(200, "<html>"))
    with pytest.raises(MalformedResponseError):
        client.transcribe(b"audio", [], [])


@pytest.mark.parametrize("exc", [requests.Timeout(), requests.ConnectionError()])
def test_network_failures_map_to_network_error(monkeypatch, exc):
    client = gemini_client.GeminiClient("key")

    def raise_it(*args, **kwargs):
        raise exc

    monkeypatch.setattr(client._session, "post", raise_it)
    with pytest.raises(NetworkError):
        client.transcribe(b"audio", [], [])


def test_user_messages_are_russian_and_key_free(monkeypatch):
    client = _client_returning(monkeypatch, _Resp(401, "key AIzaSyTOPSECRET rejected"))
    with pytest.raises(AuthError) as info:
        client.transcribe(b"audio", [], [])
    assert info.value.message == "Неверный ключ Gemini API"
    assert "AIza" not in info.value.message


def test_large_audio_goes_through_the_files_api(monkeypatch):
    calls = []

    class _Start:
        status_code = 200
        text = ""
        headers = {"x-goog-upload-url": "https://upload.example/session"}

        def json(self):
            return {}

    class _Finish:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"file": {"uri": "files/abc123"}}

    class _Interaction:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"output_text": "длинная запись"}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == gemini_client.UPLOAD_URL:
            return _Start()
        if url == "https://upload.example/session":
            return _Finish()
        return _Interaction()

    client = gemini_client.GeminiClient("key")
    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr(gemini_client, "INLINE_LIMIT_BYTES", 10)

    assert client.transcribe(b"x" * 100, [], []) == "длинная запись"
    assert [call[0] for call in calls] == [
        gemini_client.UPLOAD_URL,
        "https://upload.example/session",
        gemini_client.INTERACTIONS_URL,
    ]
    interaction_body = calls[-1][1]["json"]
    assert interaction_body["input"][0]["uri"] == "files/abc123"
    assert "data" not in interaction_body["input"][0]
