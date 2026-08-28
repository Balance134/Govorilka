"""The only module that talks to the Gemini API.

Verified against https://ai.google.dev/gemini-api/docs/transcribe and
https://ai.google.dev/gemini-api/docs/file-input-methods (checked 28.08.2026):

    POST https://generativelanguage.googleapis.com/v1beta/interactions
    x-goog-api-key: <key>
    {
      "model": "gemini-3.5-transcribe",
      "input": [{"type": "audio", "data": "<base64>", "mime_type": "audio/wav"}],
      "generation_config": {"transcription_config": {
          "mode": "smart", "custom_vocabulary": [...], "language_codes": ["ru-RU"]}}
    }

Two modes are documented: "smart" travels as the bare string, "verbatim" as
{"type": "verbatim"} - the only shape the docs ever show it in. Neither carries
timestamp_granularities or diarization_mode; those keys are never produced here,
in any mode.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Iterable

import requests

from ..config.model import DEFAULT_TRANSCRIPTION_MODE, TRANSCRIPTION_MODES
from .errors import (
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
    redact_key,
)

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com"
INTERACTIONS_URL = f"{API_BASE}/v1beta/interactions"
UPLOAD_URL = f"{API_BASE}/upload/v1beta/files"
FILES_URL = f"{API_BASE}/v1beta"
MODEL = "gemini-3.5-transcribe"
AUDIO_MIME = "audio/wav"

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 120
UPLOAD_READ_TIMEOUT = 180
FILE_TIMEOUT = 30

# Above this base64 size the payload goes through the Files API instead of
# being inlined. The number is tied to the recorder's own cap: a take is at
# most MAX_DURATION_SEC (300 s) of 16 kHz 16-bit mono, i.e. ~9.6 MB of WAV and
# ~12.8 MB of base64 (src/audio/recorder.py). Anything at or above that would
# make the upload path unreachable dead code, so the limit stays well below it
# and a dictation longer than about three minutes really goes through the Files
# API - which also means it gets deleted from Google afterwards.
# Do not raise it above 15 MB either: https://ai.google.dev/gemini-api/docs/audio
# caps the whole request at 20 MB. test_gemini_response.py pins both bounds.
INLINE_LIMIT_BYTES = 8 * 1024 * 1024

# Retries: transient statuses only, never 400/401/403. The budget below caps
# the total time spent sleeping so the app cannot look hung.
MAX_ATTEMPTS = 3
RETRY_STATUSES = (429, 500, 503)
RETRY_BASE_DELAY = 1.0
MAX_RETRY_DELAY = 8.0
RETRY_BUDGET_SEC = 20.0

# Files API: an upload is not usable until its state turns ACTIVE.
FILE_POLL_INTERVAL = 1.0
FILE_POLL_MAX_INTERVAL = 5.0
FILE_ACTIVE_DEADLINE_SEC = 120.0

DETAIL_LIMIT = 500


def _sleep(seconds: float) -> None:
    """Indirection so tests never actually wait."""
    time.sleep(seconds)


def mode_payload(transcription_mode: str) -> Any:
    """Wire shape of ``mode``; an unknown value falls back to the default.

    The fallback is the last gate before the API: a hand-edited config is
    already filtered by config.store, but nothing unknown may leave here.
    """
    mode = (
        transcription_mode
        if transcription_mode in TRANSCRIPTION_MODES
        else DEFAULT_TRANSCRIPTION_MODE
    )
    return "smart" if mode == "smart" else {"type": "verbatim"}


def build_request_body(
    audio_b64: str | None,
    vocabulary: Iterable[str],
    language_codes: Iterable[str],
    *,
    file_uri: str | None = None,
    transcription_mode: str = DEFAULT_TRANSCRIPTION_MODE,
) -> dict[str, Any]:
    """Pure request builder - unit tested, no I/O."""
    if (audio_b64 is None) == (file_uri is None):
        raise ValueError("Pass exactly one of audio_b64 / file_uri")

    if audio_b64 is not None:
        audio_input: dict[str, Any] = {
            "type": "audio",
            "data": audio_b64,
            "mime_type": AUDIO_MIME,
        }
    else:
        audio_input = {"type": "audio", "uri": file_uri, "mime_type": AUDIO_MIME}

    return {
        "model": MODEL,
        "input": [audio_input],
        "generation_config": {
            "transcription_config": {
                "mode": mode_payload(transcription_mode),
                "custom_vocabulary": list(vocabulary),
                "language_codes": list(language_codes),
            }
        },
    }


# Keys that mark a per-word timing node, which carries no transcript of its own.
_WORD_LEVEL_KEYS = ("word", "word_info", "start_time", "startTime", "end_time", "endTime")


def _collect_text(node: Any, found: list[str]) -> None:
    """Depth-first hunt for transcript text, tolerant to schema changes."""
    if isinstance(node, dict):
        for key in ("output_text", "outputText"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value)
                return
        text = node.get("text")
        node_type = node.get("type")
        is_text_node = node_type in ("text", "output_text") or (
            node_type is None and not any(key in node for key in _WORD_LEVEL_KEYS)
        )
        if isinstance(text, str) and text.strip() and is_text_node:
            found.append(text)
        for key, value in node.items():
            if key in ("text", "output_text", "outputText"):
                continue
            _collect_text(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, found)


def _snippet(payload: Any) -> str:
    """Truncated, key-free rendering of a payload for the log."""
    try:
        rendered = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(payload)
    return redact_key(rendered[:DETAIL_LIMIT])


def extract_text(payload: Any) -> str:
    """Returns the transcript. Raises on empty or unrecognisable responses."""
    if not isinstance(payload, (dict, list)):
        raise MalformedResponseError(detail=f"unexpected top-level type {type(payload)}")

    interaction = payload
    if isinstance(payload, dict) and isinstance(payload.get("interaction"), dict):
        interaction = payload["interaction"]

    found: list[str] = []
    _collect_text(interaction, found)
    text = " ".join(part.strip() for part in found if part.strip()).strip()
    if not text:
        # A blocked, truncated and genuinely silent response all land here, so
        # the payload itself is the only way to tell them apart later.
        if _looks_like_interaction(interaction):
            raise EmptyTranscriptError(
                detail=f"no text in a well-formed response: {_snippet(payload)}"
            )
        raise MalformedResponseError(
            detail=f"no recognisable text field in response: {_snippet(payload)}"
        )
    return text


def _looks_like_interaction(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in ("steps", "output", "output_text", "model", "id"))


def _error_in_payload(payload: Any) -> TranscriptionError | None:
    """A 200 can still carry {"error": {...}}; do not lose that message."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    detail = message if isinstance(message, str) and message else _snippet(error)
    code = error.get("code")
    if isinstance(code, int) and code >= 400:
        return _error_for_status(code, detail)
    return BadRequestError(detail=detail)


def _error_for_status(status: int, detail: str) -> TranscriptionError:
    if status in (401, 403):
        return AuthError(detail=detail)
    if status == 429:
        return RateLimitError(detail=detail)
    if status == 413:
        return AudioTooLargeError(detail=detail)
    if status == 400:
        lowered = detail.lower()
        if any(word in lowered for word in ("too large", "too long", "size", "exceeds")):
            return AudioTooLargeError(detail=detail)
        if any(word in lowered for word in ("audio", "mime", "format", "decode")):
            return AudioFormatError(detail=detail)
        return BadRequestError(detail=detail)
    if status >= 500:
        return ServiceUnavailableError(detail=detail)
    return BadRequestError(detail=f"HTTP {status}: {detail}")


class GeminiClient:
    """Stateless apart from the API key and a pooled HTTP session."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key.strip()
        self._session = requests.Session()

    def set_api_key(self, api_key: str) -> None:
        self._api_key = api_key.strip()

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def transcribe(
        self,
        audio_wav: bytes,
        vocabulary: list[str],
        language_codes: list[str],
        transcription_mode: str = DEFAULT_TRANSCRIPTION_MODE,
    ) -> str:
        if not self._api_key:
            raise MissingKeyError(detail="empty api key")

        audio_b64 = base64.b64encode(audio_wav).decode("ascii")
        file_name: str | None = None
        try:
            if len(audio_b64) > INLINE_LIMIT_BYTES:
                file_uri, file_name, file_info = self._upload_file(audio_wav)
                # Activation is awaited here and not inside _upload_file: the
                # name is already assigned, so a file that gets stuck in
                # PROCESSING or comes back FAILED is still deleted below.
                self._await_active(file_info, file_name)
                body = build_request_body(
                    None,
                    vocabulary,
                    language_codes,
                    file_uri=file_uri,
                    transcription_mode=transcription_mode,
                )
            else:
                body = build_request_body(
                    audio_b64,
                    vocabulary,
                    language_codes,
                    transcription_mode=transcription_mode,
                )
            return self._run_interaction(body, len(audio_wav))
        finally:
            # The recording must not sit on Google for the 48 hours the Files
            # API keeps it; a failed delete never spoils a good transcription.
            if file_name:
                self._delete_file(file_name)

    # ------------------------------------------------------------ internals
    def _run_interaction(self, body: dict[str, Any], audio_size: int) -> str:
        started = time.monotonic()
        try:
            response = self._request(
                "post",
                INTERACTIONS_URL,
                json=body,
                headers=self._headers(),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.Timeout as exc:
            raise NetworkError(detail="timeout") from exc
        except requests.RequestException as exc:
            raise NetworkError(detail=type(exc).__name__) from exc

        elapsed = time.monotonic() - started
        log.info(
            "transcribe: audio=%d bytes, status=%s, %.1fs",
            audio_size,
            response.status_code,
            elapsed,
        )

        if response.status_code >= 400:
            raise self._error_for(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedResponseError(detail="response is not JSON") from exc

        error = _error_in_payload(payload)
        if error is not None:
            raise error

        text = extract_text(payload)
        log.info("transcribe: result length=%d", len(text))
        return text

    def _request(self, method: str, url: str, **kwargs: Any) -> "requests.Response":
        """One HTTP call plus bounded retries on transient failures.

        The real bound is RETRY_BUDGET_SEC, not MAX_ATTEMPTS: the deadline is
        taken before the first call, so all MAX_ATTEMPTS attempts only happen
        when the failures are fast. One slow answer eats the budget and the
        first failure is then returned as-is, which keeps the total wait
        predictable for the app's watchdog.
        """
        call = getattr(self._session, method)
        deadline = time.monotonic() + RETRY_BUDGET_SEC
        delay = RETRY_BASE_DELAY

        for attempt in range(1, MAX_ATTEMPTS + 1):
            last_attempt = attempt == MAX_ATTEMPTS
            try:
                response = call(url, **kwargs)
            except requests.ConnectionError as exc:
                if last_attempt or not self._wait(delay, deadline):
                    raise
                log.info("retrying after %s (attempt %d)", type(exc).__name__, attempt)
                delay = min(delay * 2, MAX_RETRY_DELAY)
                continue

            if response.status_code in RETRY_STATUSES and not last_attempt:
                wait = self._retry_after(response, delay)
                if self._wait(wait, deadline):
                    log.info(
                        "retrying after HTTP %s (attempt %d)",
                        response.status_code,
                        attempt,
                    )
                    delay = min(delay * 2, MAX_RETRY_DELAY)
                    continue
            return response

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _retry_after(response: "requests.Response", fallback: float) -> float:
        raw = (response.headers or {}).get("Retry-After")
        try:
            return min(float(raw), MAX_RETRY_DELAY) if raw else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _wait(delay: float, deadline: float) -> bool:
        """Sleeps if the retry budget allows it; False means give up now."""
        if time.monotonic() + delay > deadline:
            return False
        _sleep(delay)
        return True

    @staticmethod
    def _error_for(response: "requests.Response") -> TranscriptionError:
        detail = response.text[:DETAIL_LIMIT] if response.text else ""
        return _error_for_status(response.status_code, detail)

    def _upload_file(self, audio_wav: bytes) -> tuple[str, str | None, dict[str, Any]]:
        """Resumable Files API upload, used only for oversized recordings.

        Returns (uri, name, file resource). The caller awaits activation, so
        that an upload which never becomes usable can still be deleted.
        """
        start_headers = {
            "x-goog-api-key": self._api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(audio_wav)),
            "X-Goog-Upload-Header-Content-Type": AUDIO_MIME,
            "Content-Type": "application/json",
        }
        try:
            start = self._request(
                "post",
                UPLOAD_URL,
                json={"file": {"display_name": "dictation.wav"}},
                headers=start_headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise NetworkError(detail=f"upload start: {type(exc).__name__}") from exc
        if start.status_code >= 400:
            raise self._error_for(start)

        upload_url = start.headers.get("x-goog-upload-url") or start.headers.get(
            "X-Goog-Upload-URL"
        )
        if not upload_url:
            raise MalformedResponseError(detail="Files API did not return an upload URL")

        upload_headers = {
            "Content-Length": str(len(audio_wav)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        }
        try:
            finish = self._request(
                "post",
                upload_url,
                data=audio_wav,
                headers=upload_headers,
                timeout=(CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise NetworkError(detail=f"upload finalize: {type(exc).__name__}") from exc
        if finish.status_code >= 400:
            raise self._error_for(finish)

        try:
            payload = finish.json()
        except ValueError as exc:
            raise MalformedResponseError(detail="upload response is not JSON") from exc

        file_info = self._file_resource(payload)
        uri = file_info.get("uri")
        name = file_info.get("name")
        if not isinstance(uri, str) or not uri:
            raise MalformedResponseError(detail="upload response has no file uri")
        name = name if isinstance(name, str) and name else None
        return uri, name, file_info

    @staticmethod
    def _file_resource(payload: Any) -> dict[str, Any]:
        """Files API answers either {"file": {...}} or the resource itself."""
        if not isinstance(payload, dict):
            return {}
        inner = payload.get("file")
        return inner if isinstance(inner, dict) else payload

    def _await_active(self, file_info: dict[str, Any], name: str | None) -> None:
        """Polls until the upload is usable; an unready uri gets rejected."""
        state = file_info.get("state") or "ACTIVE"
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise FileProcessingError(detail=f"file state FAILED: {_snippet(file_info)}")
        if not name:
            raise MalformedResponseError(
                detail=f"file is {state} and has no name to poll"
            )

        deadline = time.monotonic() + FILE_ACTIVE_DEADLINE_SEC
        interval = FILE_POLL_INTERVAL
        while state not in ("ACTIVE", "FAILED"):
            if time.monotonic() + interval > deadline:
                raise FileProcessingError(
                    detail=f"file stuck in {state} after {FILE_ACTIVE_DEADLINE_SEC:.0f}s"
                )
            _sleep(interval)
            interval = min(interval * 1.5, FILE_POLL_MAX_INTERVAL)
            try:
                response = self._request(
                    "get",
                    f"{FILES_URL}/{name}",
                    headers=self._headers(),
                    timeout=(CONNECT_TIMEOUT, FILE_TIMEOUT),
                )
            except requests.RequestException as exc:
                raise NetworkError(detail=f"file poll: {type(exc).__name__}") from exc
            if response.status_code >= 400:
                raise self._error_for(response)
            try:
                polled = self._file_resource(response.json())
            except ValueError as exc:
                raise MalformedResponseError(detail="file poll is not JSON") from exc
            state = polled.get("state") or "ACTIVE"

        if state == "FAILED":
            raise FileProcessingError(detail="file state FAILED while polling")

    def _delete_file(self, name: str) -> None:
        try:
            response = self._session.delete(
                f"{FILES_URL}/{name}",
                headers=self._headers(),
                timeout=(CONNECT_TIMEOUT, FILE_TIMEOUT),
            )
            if response.status_code >= 400:
                log.warning("could not delete %s: HTTP %s", name, response.status_code)
            else:
                log.info("deleted uploaded file %s", name)
        except Exception as exc:  # deleting is best effort, never fatal
            log.warning("could not delete %s: %s", name, type(exc).__name__)
