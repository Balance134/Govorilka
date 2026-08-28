"""Microphone capture into memory as 16 kHz mono 16-bit WAV."""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import Optional

import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes, PCM 16 bit
MIN_DURATION_SEC = 0.3
BLOCK_SIZE = 1024


class MicrophoneError(RuntimeError):
    """Raised with a user-facing Russian message."""


class Recorder:
    """Non-blocking capture: audio arrives on the PortAudio callback thread."""

    def __init__(self) -> None:
        self._stream: Optional[sd.RawInputStream] = None
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._failed = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks = []
            self._failed = False
        try:
            # RawInputStream hands over plain bytes, so numpy is not needed.
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:  # sounddevice raises several unrelated types
            log.exception("Failed to open the input stream")
            self._stream = None
            raise MicrophoneError("Микрофон недоступен") from exc
        self._stream = stream

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            # Overflows are survivable; a device error is not.
            log.debug("Audio callback status: %s", status)
            if getattr(status, "input_error", False):
                self._failed = True
        with self._lock:
            self._chunks.append(bytes(indata))

    def stop(self) -> bytes:
        """Stops capture and returns a complete WAV file as bytes."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks = []
            failed = self._failed
        if failed and not pcm:
            raise MicrophoneError("Микрофон недоступен")
        return encode_wav(pcm)

    def abort(self) -> None:
        """Drop the recording without producing audio (used on shutdown)."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.exception("Failed to stop the input stream")
            finally:
                try:
                    stream.close()
                except Exception:
                    log.exception("Failed to close the input stream")
        with self._lock:
            self._chunks = []


def encode_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate() or SAMPLE_RATE
    return frames / float(rate)


def is_too_short(wav_bytes: bytes) -> bool:
    try:
        return wav_duration_seconds(wav_bytes) < MIN_DURATION_SEC
    except wave.Error:
        return True
