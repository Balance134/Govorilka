"""Microphone capture into memory as 16 kHz mono 16-bit WAV."""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import NamedTuple, Optional

import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes, PCM 16 bit
MIN_DURATION_SEC = 0.3
BLOCK_SIZE = 1024

# Hard cap on a single take. A hold that is never released (a UAC prompt
# steals the key-up) must not record until the disk fills up.
MAX_DURATION_SEC = 300.0
MAX_CHUNKS = int(MAX_DURATION_SEC * SAMPLE_RATE / BLOCK_SIZE) + 1

MAX_SAMPLE = 32767
# The level meter looks at every fourth sample only. Windows drops a slow audio
# callback, and a quarter of the block is still one reading per 0.25 ms - far
# more than a 30 FPS indicator can show.
LEVEL_STRIDE = 4

CAP_WARNING = "Достигнут предел длины записи — 5 минут"
DEVICE_LOST_WARNING = "Микрофон пропал во время записи, распознана только часть"


class MicrophoneError(RuntimeError):
    """Raised with a user-facing Russian message."""


class Recording(NamedTuple):
    """Result of a take: the WAV plus a Russian warning when it is not clean."""

    wav: bytes
    warning: Optional[str] = None


class Recorder:
    """Non-blocking capture: audio arrives on the PortAudio callback thread."""

    def __init__(self) -> None:
        self._stream: Optional[sd.RawInputStream] = None
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._failed = False
        self._capped = False
        self._stopping = False
        self._peak = 0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            # Defence in depth: the app no longer gets here, because a second
            # hotkey press during RECORDING is already refused upstream. Never
            # reuse a half-full buffer either way - the caller thinks it starts
            # a fresh take and would get the previous one appended to it.
            log.error("Recorder.start() called while a stream is already open")
            raise MicrophoneError("Запись уже идёт")
        with self._lock:
            self._chunks = []
            self._failed = False
            self._capped = False
            self._stopping = False
            self._peak = 0
        try:
            # RawInputStream hands over plain bytes, so numpy is not needed.
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()
        except Exception as exc:  # sounddevice raises several unrelated types
            log.exception("Failed to open the input stream")
            self._stream = None
            raise MicrophoneError("Микрофон недоступен") from exc
        self._stream = stream

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            # CallbackFlags only exposes overflow/underflow/priming; those are
            # survivable hiccups, so they are logged and nothing more. A device
            # that disappears shows up as an exception in stop().
            log.debug("Audio callback status: %s", status)
        chunk = bytes(indata)
        peak = block_peak(chunk)
        with self._lock:
            # The meter keeps running past the cap: the microphone is still
            # open, so the indicator must not freeze.
            self._peak = peak
            if len(self._chunks) >= MAX_CHUNKS:
                self._capped = True
                return
            self._chunks.append(chunk)

    def peak_level(self) -> int:
        """Loudest sample of the most recent block, 0..MAX_SAMPLE.

        Cheap and thread-safe: the GUI polls it about 30 times a second.
        """
        with self._lock:
            return self._peak

    def _on_stream_finished(self) -> None:
        """PortAudio ends the stream on its own when the device disappears."""
        with self._lock:
            if self._stopping:
                return  # we asked for it, nothing is wrong
            log.warning("Input stream finished on its own - device lost")
            self._failed = True

    def stop(self) -> Recording:
        """Stops capture and returns a complete WAV file plus a warning."""
        stream = self._stream
        self._stream = None
        device_lost = False
        with self._lock:
            self._stopping = True
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.exception("Failed to stop the input stream")
                device_lost = True
            try:
                stream.close()
            except Exception:
                log.exception("Failed to close the input stream")
                device_lost = True
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks = []
            device_lost = device_lost or self._failed
            capped = self._capped
            self._failed = False
            self._capped = False
            self._stopping = False
            self._peak = 0
        if device_lost and not pcm:
            raise MicrophoneError("Микрофон недоступен")
        warning = None
        if device_lost:
            warning = DEVICE_LOST_WARNING
        elif capped:
            warning = CAP_WARNING
        return Recording(encode_wav(pcm), warning)

    def abort(self) -> None:
        """Drop the recording without producing audio (used on shutdown)."""
        stream = self._stream
        self._stream = None
        with self._lock:
            self._stopping = True
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
            self._failed = False
            self._capped = False
            self._stopping = False
            self._peak = 0


def block_peak(pcm: bytes) -> int:
    """Peak amplitude of one 16-bit mono block, 0..MAX_SAMPLE.

    Two C-level passes over a strided view: about 16 us for a 1024-frame block
    against the 64 ms that block covers.
    """
    try:
        samples = memoryview(pcm).cast("h")[::LEVEL_STRIDE]
    except (TypeError, ValueError):
        log.debug("Could not read the audio block as int16", exc_info=True)
        return 0
    if not len(samples):
        return 0
    high = max(samples)
    low = min(samples)
    peak = high if high >= -low else -low
    return min(peak, MAX_SAMPLE)


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
