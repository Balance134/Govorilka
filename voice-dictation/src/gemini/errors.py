"""Typed transcription errors. ``message`` is always shown to the user."""

from __future__ import annotations


class TranscriptionError(Exception):
    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail  # goes to the log only


class AuthError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Неверный ключ Gemini API", detail=detail)


class RateLimitError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Превышен лимит запросов к Gemini API", detail=detail)


class AudioFormatError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Неподдерживаемый формат аудио", detail=detail)


class BadRequestError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Gemini API отклонил запрос", detail=detail)


class ServiceUnavailableError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Сервис Gemini временно недоступен", detail=detail)


class NetworkError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Не удалось связаться с Gemini API", detail=detail)


class EmptyTranscriptError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Речь не распознана", detail=detail)


class MalformedResponseError(TranscriptionError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("Некорректный ответ Gemini API", detail=detail)
