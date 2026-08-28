"""Typed transcription errors. ``message`` is always shown to the user."""

from __future__ import annotations

import re

# API keys look like AIza + 35 chars. Anything starting with the prefix is
# scrubbed, whatever its length, so a truncated key cannot reach the log file.
_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]*")


def redact_key(text: str) -> str:
    """Removes anything that looks like a Gemini API key."""
    return _API_KEY_RE.sub("[REDACTED_KEY]", text)


class TranscriptionError(Exception):
    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = redact_key(detail)  # goes to the log only


class AuthError(TranscriptionError):
    def __init__(self, detail: str = "", message: str = "Неверный ключ Gemini API") -> None:
        super().__init__(message, detail=detail)


class MissingKeyError(AuthError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(detail, message="Ключ Gemini API не указан — откройте настройки")


class RateLimitError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Превышен лимит запросов к Gemini API", detail=detail)


class AudioFormatError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Неподдерживаемый формат аудио", detail=detail)


class AudioTooLargeError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "Запись слишком длинная для Gemini API — надиктуйте покороче",
            detail=detail,
        )


class BadRequestError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Gemini API отклонил запрос", detail=detail)


class ServiceUnavailableError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Сервис Gemini временно недоступен", detail=detail)


class NetworkError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Не удалось связаться с Gemini API", detail=detail)


class FileProcessingError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "Gemini не смог подготовить запись — попробуйте ещё раз",
            detail=detail,
        )


class EmptyTranscriptError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Речь не распознана", detail=detail)


class MalformedResponseError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Некорректный ответ Gemini API", detail=detail)
