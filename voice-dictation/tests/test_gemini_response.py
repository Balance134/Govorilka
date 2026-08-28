import pytest
import requests

from src.gemini import client as gemini_client
from src.gemini.errors import (
    AudioFormatError,
    AudioTooLargeError,
    AuthError,
    BadRequestError,
    EmptyTranscriptError,
    FileProcessingError,
    MalformedResponseError,
    MissingKeyError,
    NetworkError,
    RateLimitError,
    ServiceUnavailableError,
    TranscriptionError,
)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """No test may actually wait; the recorded delays are asserted instead."""
    recorded = []
    monkeypatch.setattr(gemini_client, "_sleep", recorded.append)
    return recorded


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
    def __init__(self, status_code, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {}

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
    assert "AIza" not in info.value.detail
    assert "TOPSECRET" not in info.value.detail


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


# ------------------------------------------------------------- Files API flow
UPLOAD_SESSION = "https://upload.example/session"


def _files_api(monkeypatch, client, *, file_info, poll_states=(), interaction=None,
               on_delete=None):
    """Fake transport for the whole upload -> transcribe -> delete round trip."""
    calls = []
    polls = iter(poll_states)

    def post(url, **kwargs):
        calls.append(("post", url, kwargs))
        if url == gemini_client.UPLOAD_URL:
            return _Resp(200, payload={}, headers={"x-goog-upload-url": UPLOAD_SESSION})
        if url == UPLOAD_SESSION:
            return _Resp(200, payload={"file": file_info})
        return interaction or _Resp(200, payload={"output_text": "длинная запись"})

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return _Resp(200, payload={"state": next(polls)})

    def delete(url, **kwargs):
        calls.append(("delete", url, kwargs))
        if on_delete is not None:
            return on_delete()
        return _Resp(200, payload={})

    monkeypatch.setattr(client._session, "post", post)
    monkeypatch.setattr(client._session, "get", get)
    monkeypatch.setattr(client._session, "delete", delete)
    monkeypatch.setattr(gemini_client, "INLINE_LIMIT_BYTES", 10)
    return calls


def test_processing_file_is_polled_until_active(monkeypatch, sleeps):
    client = gemini_client.GeminiClient("key")
    calls = _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc123", "name": "files/abc123", "state": "PROCESSING"},
        poll_states=["PROCESSING", "ACTIVE"],
    )

    assert client.transcribe(b"x" * 100, [], []) == "длинная запись"
    polls = [url for method, url, _ in calls if method == "get"]
    assert polls == [f"{gemini_client.FILES_URL}/files/abc123"] * 2
    # The interaction must come after the last poll, not before it.
    assert [method for method, _, _ in calls] == [
        "post", "post", "get", "get", "post", "delete",
    ]
    assert calls[-2][1] == gemini_client.INTERACTIONS_URL
    assert sleeps and all(delay > 0 for delay in sleeps)


def test_failed_file_state_is_reported(monkeypatch):
    client = gemini_client.GeminiClient("key")
    calls = _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc", "name": "files/abc", "state": "FAILED"},
    )
    with pytest.raises(FileProcessingError):
        client.transcribe(b"x" * 100, [], [])
    assert not [url for method, url, _ in calls if url == gemini_client.INTERACTIONS_URL]


def test_file_never_becoming_active_fails_with_a_deadline(monkeypatch):
    client = gemini_client.GeminiClient("key")
    monkeypatch.setattr(gemini_client, "FILE_ACTIVE_DEADLINE_SEC", 0.0)
    _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc", "name": "files/abc", "state": "PROCESSING"},
        poll_states=["PROCESSING"] * 10,
    )
    with pytest.raises(FileProcessingError) as info:
        client.transcribe(b"x" * 100, [], [])
    assert "PROCESSING" in info.value.detail
    assert info.value.message == "Gemini не смог подготовить запись — попробуйте ещё раз"


def test_uploaded_file_is_deleted_afterwards(monkeypatch):
    client = gemini_client.GeminiClient("key")
    calls = _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc123", "name": "files/abc123", "state": "ACTIVE"},
    )
    assert client.transcribe(b"x" * 100, [], []) == "длинная запись"
    assert calls[-1][0] == "delete"
    assert calls[-1][1] == f"{gemini_client.FILES_URL}/files/abc123"


def test_delete_is_attempted_even_when_transcription_fails(monkeypatch):
    client = gemini_client.GeminiClient("key")
    calls = _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc", "name": "files/abc", "state": "ACTIVE"},
        interaction=_Resp(400, "nope"),
    )
    with pytest.raises(BadRequestError):
        client.transcribe(b"x" * 100, [], [])
    assert calls[-1][0] == "delete"


def test_failed_delete_does_not_break_a_good_transcription(monkeypatch):
    client = gemini_client.GeminiClient("key")

    def boom():
        raise requests.ConnectionError("no route")

    _files_api(
        monkeypatch,
        client,
        file_info={"uri": "files/abc", "name": "files/abc", "state": "ACTIVE"},
        on_delete=boom,
    )
    assert client.transcribe(b"x" * 100, [], []) == "длинная запись"


# ------------------------------------------------------------------- retries
def _sequence_client(monkeypatch, responses):
    client = gemini_client.GeminiClient("key")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client._session, "post", post)
    return client, calls


def test_transient_503_is_retried(monkeypatch, sleeps):
    client, calls = _sequence_client(
        monkeypatch,
        [_Resp(503, "unavailable"), _Resp(200, payload={"output_text": "получилось"})],
    )
    assert client.transcribe(b"audio", [], []) == "получилось"
    assert len(calls) == 2
    assert sleeps == [gemini_client.RETRY_BASE_DELAY]


def test_connection_error_is_retried(monkeypatch, sleeps):
    client, calls = _sequence_client(
        monkeypatch,
        [requests.ConnectionError("reset"), _Resp(200, payload={"output_text": "ок"})],
    )
    assert client.transcribe(b"audio", [], []) == "ок"
    assert len(calls) == 2


def test_retry_after_header_is_honoured(monkeypatch, sleeps):
    client, calls = _sequence_client(
        monkeypatch,
        [
            _Resp(429, "slow down", headers={"Retry-After": "3"}),
            _Resp(200, payload={"output_text": "ок"}),
        ],
    )
    assert client.transcribe(b"audio", [], []) == "ок"
    assert sleeps == [3.0]


def test_retries_are_bounded_and_then_surface(monkeypatch, sleeps):
    client, calls = _sequence_client(monkeypatch, [_Resp(503, "unavailable")])
    with pytest.raises(ServiceUnavailableError):
        client.transcribe(b"audio", [], [])
    assert len(calls) == gemini_client.MAX_ATTEMPTS
    assert sum(sleeps) <= gemini_client.RETRY_BUDGET_SEC


@pytest.mark.parametrize("status", [400, 401, 403])
def test_client_errors_are_never_retried(monkeypatch, status, sleeps):
    client, calls = _sequence_client(monkeypatch, [_Resp(status, "no")])
    with pytest.raises(TranscriptionError):
        client.transcribe(b"audio", [], [])
    assert len(calls) == 1
    assert sleeps == []


# ------------------------------------------------------- error bodies, sizes
def test_error_body_in_a_200_reaches_the_detail(monkeypatch):
    payload = {"error": {"code": 400, "message": "audio content is unintelligible"}}
    client = _client_returning(monkeypatch, _Resp(200, payload=payload))
    with pytest.raises(AudioFormatError) as info:
        client.transcribe(b"audio", [], [])
    assert "unintelligible" in info.value.detail


def test_error_body_without_a_code_is_a_bad_request(monkeypatch):
    payload = {"error": {"message": "key AIzaSyTOPSECRETKEY refused"}}
    client = _client_returning(monkeypatch, _Resp(200, payload=payload))
    with pytest.raises(BadRequestError) as info:
        client.transcribe(b"audio", [], [])
    assert "refused" in info.value.detail
    assert "AIza" not in info.value.detail


def test_empty_transcript_keeps_the_payload_in_the_detail(monkeypatch):
    payload = {"id": "abc", "steps": [], "finish_reason": "SAFETY"}
    client = _client_returning(monkeypatch, _Resp(200, payload=payload))
    with pytest.raises(EmptyTranscriptError) as info:
        client.transcribe(b"audio", [], [])
    assert "SAFETY" in info.value.detail


def test_payload_too_large_is_its_own_message(monkeypatch):
    client = _client_returning(monkeypatch, _Resp(413, "payload too large"))
    with pytest.raises(AudioTooLargeError) as info:
        client.transcribe(b"audio", [], [])
    assert info.value.message == "Запись слишком длинная для Gemini API — надиктуйте покороче"


def test_400_mentioning_size_is_too_large(monkeypatch):
    client = _client_returning(
        monkeypatch, _Resp(400, "request payload size exceeds the limit")
    )
    with pytest.raises(AudioTooLargeError):
        client.transcribe(b"audio", [], [])


def test_unmapped_status_falls_through_to_bad_request():
    error = gemini_client.GeminiClient._error_for(_Resp(404, "not found"))
    assert isinstance(error, BadRequestError)
    assert error.detail == "HTTP 404: not found"


def test_413_maps_to_too_large_in_error_for():
    error = gemini_client.GeminiClient._error_for(_Resp(413, "too large"))
    assert isinstance(error, AudioTooLargeError)


def test_missing_key_differs_from_a_wrong_key():
    with pytest.raises(MissingKeyError) as info:
        gemini_client.GeminiClient("").transcribe(b"x", [], [])
    assert info.value.message == "Ключ Gemini API не указан — откройте настройки"
    assert isinstance(info.value, AuthError)
