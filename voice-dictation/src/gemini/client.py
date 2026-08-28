"""The only module that talks to the Gemini API.

Verified against https://ai.google.dev/gemini-api/docs/transcribe and
https://ai.google.dev/gemini-api/docs/file-input-methods (checked 28.08.2026):

    POST https://generativelanguage.googleapis.com/v1beta/interactions
    x-goog-api-key: <key>
    {
      "model": "gemini-3.5-transcribe",
      "input": [{"type": "audio", "data": "<base64>", "mime_type": "audio/wav"}],
      "generation_config": {"transcription_config": {
          "mode": "smart", "custom_vocabulary": [...], "language_codes": []}}
    }

"smart" is a string and must not be combined with timestamp_granularities or
diarization_mode - neither key is ever produced here.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Iterable

import requests

from .errors import (
    AudioFormatError,
    AuthError,
    BadRequestError,
    EmptyTranscriptError,
    MalformedResponseError,
    NetworkError,
    RateLimitError,
    ServiceUnavailableError,
    TranscriptionError,
)

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com"
INTERACTIONS_URL = f"{API_BASE}/v1beta/interactions"
UPLOAD_URL = f"{API_BASE}/upload/v1beta/files"
MODEL = "gemini-3.5-transcribe"
AUDIO_MIME = "audio/wav"

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 120
UPLOAD_READ_TIMEOUT = 180

# Above this base64 size the payload goes through the Files API instead of
# being inlined. Dictation never reaches it; long recordings can.
INLINE_LIMIT_BYTES = 15 * 1024 * 1024


def build_request_body(
    audio_b64: str | None,
    vocabulary: Iterable[str],
    language_codes: Iterable[str],
    *,
    file_uri: str | None = None,
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
                "mode": "smart",
                "custom_vocabulary": list(vocabulary),
                "language_codes": list(language_codes),
            }
        },
    }


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
        if isinstance(text, str) and text.strip() and node_type in (None, "text", "output_text"):
            found.append(text)
        for key, value in node.items():
            if key in ("text", "output_text", "outputText"):
                continue
            _collect_text(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, found)


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
        if _looks_like_interaction(interaction):
            raise EmptyTranscriptError(detail="no text in a well-formed response")
        raise MalformedResponseError(detail="no recognisable text field in response")
    return text


def _looks_like_interaction(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in ("steps", "output", "output_text", "model", "id"))


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
    ) -> str:
        if not self._api_key:
            raise AuthError(detail="empty api key")

        audio_b64 = base64.b64encode(audio_wav).decode("ascii")
        if len(audio_b64) > INLINE_LIMIT_BYTES:
            file_uri = self._upload_file(audio_wav)
            body = build_request_body(
                None, vocabulary, language_codes, file_uri=file_uri
            )
        else:
            body = build_request_body(audio_b64, vocabulary, language_codes)

        started = time.monotonic()
        try:
            response = self._session.post(
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
            len(audio_wav),
            response.status_code,
            elapsed,
        )

        if response.status_code >= 400:
            raise self._error_for(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedResponseError(detail="response is not JSON") from exc

        text = extract_text(payload)
        log.info("transcribe: result length=%d", len(text))
        return text

    # ------------------------------------------------------------ internals
    @staticmethod
    def _error_for(response: "requests.Response") -> TranscriptionError:
        status = response.status_code
        detail = response.text[:500] if response.text else ""
        if status in (401, 403):
            return AuthError(detail=detail)
        if status == 429:
            return RateLimitError(detail=detail)
        if status == 400:
            lowered = detail.lower()
            if any(word in lowered for word in ("audio", "mime", "format", "decode")):
                return AudioFormatError(detail=detail)
            return BadRequestError(detail=detail)
        if status >= 500:
            return ServiceUnavailableError(detail=detail)
        return BadRequestError(detail=f"HTTP {status}: {detail}")

    def _upload_file(self, audio_wav: bytes) -> str:
        """Resumable Files API upload, used only for oversized recordings."""
        start_headers = {
            "x-goog-api-key": self._api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(audio_wav)),
            "X-Goog-Upload-Header-Content-Type": AUDIO_MIME,
            "Content-Type": "application/json",
        }
        try:
            start = self._session.post(
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
            finish = self._session.post(
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

        file_info = payload.get("file") if isinstance(payload, dict) else None
        uri = None
        if isinstance(file_info, dict):
            uri = file_info.get("uri")
        if not isinstance(uri, str) or not uri:
            raise MalformedResponseError(detail="upload response has no file uri")
        return uri
