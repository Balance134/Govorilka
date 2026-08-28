"""Recorder lifecycle and the Qt-free decisions the coordinator makes on it.

PySide6 and sounddevice are not importable here (and must never be required by
the suite), so `sounddevice` is replaced by a stub before the recorder is
imported and every stream is a fake. The Qt parts - the duration QTimer, the
busy watchdog, the shutdown path - are exercised through `src.app_logic`, where
the decisions their slots make are kept.
"""

import sys
import types

import pytest

if "sounddevice" not in sys.modules:  # pragma: no cover - depends on the box
    stub = types.ModuleType("sounddevice")
    stub.RawInputStream = object
    sys.modules["sounddevice"] = stub

from src import app_logic
from src.app_logic import (
    BAR_COUNT,
    BAR_DECAY_PER_FRAME,
    BAR_FLOOR,
    DictationTimings,
    IDLE_BARS,
    LATE_RESULT_NOTICE,
    MAX_RECORDING_MS,
    StopOutcome,
    TakeGuard,
    TextOutcome,
    busy_message,
    decide_stop,
    decide_text_ready,
    format_timings,
    next_bar_heights,
    peak_to_level,
    safe_apply_replacements,
)
from src.audio import recorder as recorder_module
from src.audio.recorder import (
    CAP_WARNING,
    DEVICE_LOST_WARNING,
    MAX_CHUNKS,
    MAX_DURATION_SEC,
    MAX_SAMPLE,
    BLOCK_SIZE,
    MicrophoneError,
    Recorder,
    SAMPLE_RATE,
    block_peak,
    wav_duration_seconds,
)


class FakeCallbackFlags:
    """Mirrors sounddevice 0.5.6: __slots__, and no `input_error` attribute."""

    __slots__ = ("_flags",)

    def __init__(self, flags: int = 0) -> None:
        self._flags = flags

    def __bool__(self) -> bool:
        return bool(self._flags)

    def __str__(self) -> str:
        return "input overflow" if self._flags else ""

    @property
    def input_underflow(self) -> bool:
        return bool(self._flags & 1)

    @property
    def input_overflow(self) -> bool:
        return bool(self._flags & 2)

    @property
    def output_underflow(self) -> bool:
        return False

    @property
    def output_overflow(self) -> bool:
        return False

    @property
    def priming_output(self) -> bool:
        return False


class FakeStream:
    def __init__(self, *, stop_raises: bool = False, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self._stop_raises = stop_raises
        self.finished_callback = kwargs.get("finished_callback")

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        if self._stop_raises:
            raise RuntimeError("PortAudio: device unavailable")
        self.started = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def rec(monkeypatch):
    """A recorder whose streams are fakes; `rec.streams` collects them."""
    created = []

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    monkeypatch.setattr(recorder_module.sd, "RawInputStream", factory, raising=False)
    recorder = Recorder()
    recorder.streams = created
    return recorder


def feed(recorder: Recorder, blocks: int) -> None:
    block = b"\x01\x02" * BLOCK_SIZE
    for _ in range(blocks):
        recorder._callback(block, BLOCK_SIZE, None, FakeCallbackFlags())


# ------------------------------------------------------------------ #7 flags
def test_real_callback_flags_have_no_input_error():
    """The old `getattr(status, "input_error", False)` branch was dead code."""
    status = FakeCallbackFlags(2)
    assert not hasattr(status, "input_error")
    with pytest.raises(AttributeError):
        status.input_error = True  # __slots__, cannot be faked into existence


def test_overflow_alone_is_not_a_device_failure(rec):
    rec.start()
    block = b"\x00\x00" * BLOCK_SIZE
    for _ in range(10):
        rec._callback(block, BLOCK_SIZE, None, FakeCallbackFlags(2))
    result = rec.stop()
    assert result.warning is None
    assert wav_duration_seconds(result.wav) > 0


def test_stream_finished_on_its_own_reports_a_lost_device(rec):
    rec.start()
    feed(rec, 20)
    rec.streams[0].finished_callback()  # PortAudio drops a removed device
    result = rec.stop()
    assert result.warning == DEVICE_LOST_WARNING


def test_failure_while_stopping_is_reported(rec, monkeypatch):
    rec.start()
    feed(rec, 20)
    monkeypatch.setattr(
        rec.streams[0], "stop", lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    result = rec.stop()
    assert result.warning == DEVICE_LOST_WARNING


# ------------------------------------------------------- #8 partial audio
def test_partial_audio_is_returned_with_a_warning(rec):
    rec.start()
    feed(rec, 200)
    rec.streams[0].finished_callback()
    result = rec.stop()
    assert wav_duration_seconds(result.wav) > 0  # the half sentence is kept
    assert result.warning  # but the user is told it is a half sentence


def test_lost_device_without_any_audio_raises(rec):
    rec.start()
    rec.streams[0].finished_callback()
    with pytest.raises(MicrophoneError):
        rec.stop()


# ------------------------------------------------------ #10 double start
def test_start_while_open_raises_and_keeps_the_stale_buffer_out(rec):
    rec.start()
    feed(rec, 100)
    first_stream = rec.streams[0]
    with pytest.raises(MicrophoneError):
        rec.start()
    assert len(rec.streams) == 1  # no second device was opened
    assert rec._stream is first_stream
    # The previous take is still there, which is exactly why a silent restart
    # would have transcribed it as if it were the new one.
    assert wav_duration_seconds(rec.stop().wav) > 0


def test_start_after_stop_begins_an_empty_take(rec):
    rec.start()
    feed(rec, 100)
    rec.stop()
    rec.start()
    assert wav_duration_seconds(rec.stop().wav) == 0


# --------------------------------------------------------- #3 duration cap
def test_chunk_cap_matches_the_documented_five_minutes():
    capped_seconds = MAX_CHUNKS * BLOCK_SIZE / float(SAMPLE_RATE)
    assert MAX_DURATION_SEC == 300.0
    assert capped_seconds == pytest.approx(MAX_DURATION_SEC, abs=1.0)


def test_recording_stops_growing_at_the_cap(rec):
    rec.start()
    feed(rec, MAX_CHUNKS + 50)
    result = rec.stop()
    assert wav_duration_seconds(result.wav) <= MAX_DURATION_SEC + 0.1
    assert result.warning == CAP_WARNING


def test_cap_warning_only_when_the_cap_is_hit(rec):
    rec.start()
    feed(rec, 10)
    assert rec.stop().warning is None


# ------------------------------------------------------------ #9 shutdown
def test_abort_is_idempotent_and_closes_the_device(rec):
    rec.start()
    feed(rec, 10)
    rec.abort()
    rec.abort()
    assert rec.streams[0].closed
    assert not rec.is_recording
    rec.start()
    assert wav_duration_seconds(rec.stop().wav) == 0


# ------------------------------------------- #2 the cap must keep the speech
def test_the_app_timer_fires_after_the_recorder_reaches_its_own_cap():
    """Otherwise the recorder's cap is dead code and the take is thrown away."""
    assert MAX_RECORDING_MS > MAX_DURATION_SEC * 1000


def test_a_take_that_hit_the_cap_is_still_transcribed():
    outcome = decide_stop(CAP_WARNING, too_short=False, timed_out=True)
    assert outcome.transcribe is True  # five minutes of speech are not lost
    assert "5 минут" in outcome.notice


def test_a_timed_out_take_reports_a_lost_device_too():
    outcome = decide_stop(DEVICE_LOST_WARNING, too_short=False, timed_out=True)
    assert outcome.transcribe is True
    assert DEVICE_LOST_WARNING in outcome.notice


# --------------------------------------------------- #10 one balloon per take
def test_a_lost_device_and_a_short_take_share_one_notice():
    outcome = decide_stop(DEVICE_LOST_WARNING, too_short=True)
    assert outcome.transcribe is False
    assert outcome.notice.count(DEVICE_LOST_WARNING) == 1
    assert "короткая" in outcome.notice  # both reasons, one balloon


def test_a_clean_take_says_nothing():
    assert decide_stop(None, too_short=False) == StopOutcome(True, None)


def test_a_short_take_alone_says_only_that():
    assert decide_stop(None, too_short=True).notice == "Слишком короткая запись"


# ------------------------------------------ #4 nothing starts after cleanup
def test_a_transcript_arriving_after_shutdown_is_dropped():
    outcome = decide_text_ready(
        shutting_down=True, is_current_take=True, text="привет"
    )
    assert outcome is TextOutcome.DROP_SHUTDOWN


def test_a_normal_transcript_is_injected():
    outcome = decide_text_ready(
        shutting_down=False, is_current_take=True, text="привет"
    )
    assert outcome is TextOutcome.INJECT


def test_an_empty_transcript_is_an_error():
    outcome = decide_text_ready(
        shutting_down=False, is_current_take=True, text="   "
    )
    assert outcome is TextOutcome.EMPTY


# --------------------------------------- #6 a late transcript is never silent
def test_a_transcript_of_an_abandoned_take_is_reported():
    outcome = decide_text_ready(
        shutting_down=False, is_current_take=False, text="привет"
    )
    assert outcome is TextOutcome.DROP_LATE
    assert LATE_RESULT_NOTICE  # the user is told, not left waiting


def test_the_watchdog_makes_the_running_take_stale():
    guard = TakeGuard()
    generation = guard.begin()
    assert guard.is_current(generation)
    guard.abandon()  # what the busy watchdog does
    assert not guard.is_current(generation)
    assert guard.is_current(guard.begin())


# ------------------------------------ #7 the busy message tells the truth
def test_the_busy_message_depends_on_the_state():
    from src.utils.state import AppState

    assert "запись" in busy_message(AppState.RECORDING).lower()
    assert busy_message(AppState.RECORDING) != busy_message(AppState.PROCESSING)
    assert "обработка" in busy_message(AppState.PROCESSING).lower()


# ------------------------ #13 a broken replacement table costs no dictation
def test_a_failing_replacement_table_falls_back_to_the_raw_transcript(monkeypatch):
    def boom(text, rules):
        raise ValueError("case folding mismatch")

    monkeypatch.setattr(app_logic, "apply_replacements", boom)
    assert safe_apply_replacements("привет мир", []) == "привет мир"


def test_replacements_are_applied_when_the_table_works(monkeypatch):
    monkeypatch.setattr(app_logic, "apply_replacements", lambda text, rules: text.upper())
    assert safe_apply_replacements("привет", []) == "ПРИВЕТ"


# ---------------------------------------------- recording indicator: metering
def loud_block(value: int = 20_000) -> bytes:
    return value.to_bytes(2, "little", signed=True) * BLOCK_SIZE


def test_a_silent_block_reads_as_zero():
    assert block_peak(b"\x00\x00" * BLOCK_SIZE) == 0


def test_the_peak_ignores_the_sign_of_the_sample():
    quiet_but_negative = (-9000).to_bytes(2, "little", signed=True) * BLOCK_SIZE
    assert block_peak(quiet_but_negative) == 9000


def test_the_peak_never_leaves_the_16_bit_range():
    assert block_peak((-32768).to_bytes(2, "little", signed=True) * 8) == MAX_SAMPLE


def test_an_odd_sized_block_does_not_kill_the_callback():
    assert block_peak(b"\x01\x02\x03") == 0


def test_the_recorder_publishes_the_level_of_the_last_block(rec):
    rec.start()
    assert rec.peak_level() == 0
    rec._callback(loud_block(), BLOCK_SIZE, None, FakeCallbackFlags())
    assert rec.peak_level() == 20_000
    rec._callback(b"\x00\x00" * BLOCK_SIZE, BLOCK_SIZE, None, FakeCallbackFlags())
    assert rec.peak_level() == 0  # silence must show as silence, not as decay


def test_the_level_keeps_moving_after_the_cap(rec):
    """The microphone is still open at the cap, so the indicator must not freeze."""
    rec.start()
    feed(rec, MAX_CHUNKS)
    rec._callback(loud_block(), BLOCK_SIZE, None, FakeCallbackFlags())
    assert rec.peak_level() == 20_000


def test_a_finished_take_leaves_the_level_at_rest(rec):
    rec.start()
    rec._callback(loud_block(), BLOCK_SIZE, None, FakeCallbackFlags())
    rec.stop()
    assert rec.peak_level() == 0


# ------------------------------------------------- recording indicator: bars
def test_silence_is_the_floor_and_full_scale_is_the_top():
    assert peak_to_level(0) == 0.0
    assert peak_to_level(MAX_SAMPLE) == pytest.approx(1.0)


def test_the_scale_is_logarithmic_not_linear():
    """Half of full scale is -6 dB, which must land near the top, not at 0.5."""
    assert peak_to_level(MAX_SAMPLE // 2) > 0.85


def test_a_barely_audible_sample_counts_as_silence():
    assert peak_to_level(5) == 0.0


def test_silence_leaves_every_bar_on_the_floor():
    bars = next_bar_heights(IDLE_BARS, 0.0, frame=3)
    assert len(bars) == BAR_COUNT
    assert bars == IDLE_BARS


def test_a_loud_take_pushes_a_bar_to_the_top():
    bars = IDLE_BARS
    tallest = 0.0
    for frame in range(len(app_logic.BAR_RIPPLE)):
        bars = next_bar_heights(bars, 1.0, frame)
        tallest = max(tallest, max(bars))
    assert tallest == pytest.approx(1.0)
    assert min(bars) > BAR_FLOOR  # every bar is alive while speech goes on


def test_the_bars_are_not_all_the_same_height():
    bars = next_bar_heights(IDLE_BARS, 0.8, frame=0)
    assert len(set(round(bar, 3) for bar in bars)) > 1


def test_a_bar_falls_by_one_step_per_frame_instead_of_dropping_out():
    full = (1.0,) * BAR_COUNT
    after = next_bar_heights(full, 0.0, frame=0)
    assert all(bar == pytest.approx(1.0 - BAR_DECAY_PER_FRAME) for bar in after)
    assert all(bar > BAR_FLOOR for bar in after)


def test_the_decay_ends_on_the_floor_and_stays_there():
    bars = (1.0,) * BAR_COUNT
    for frame in range(50):
        bars = next_bar_heights(bars, 0.0, frame)
    assert bars == IDLE_BARS


def test_a_bar_rises_at_once_when_the_voice_comes_back():
    quiet = next_bar_heights(IDLE_BARS, 0.0, frame=0)
    loud = next_bar_heights(quiet, 1.0, frame=0)
    assert max(loud) > 0.5  # attack is immediate, only the fall is smoothed


def test_out_of_range_levels_are_clamped():
    assert next_bar_heights(IDLE_BARS, -5.0, frame=0) == IDLE_BARS
    assert max(next_bar_heights(IDLE_BARS, 99.0, frame=2)) <= 1.0


# ---------------------------------------------------------------- timings
def test_the_timing_line_names_every_stage():
    line = format_timings(4.12, 2.74, 0.21, chars=57)
    assert line == "dictation timings: audio=4.1s transcribe=2.7s inject=0.2s chars=57"


def test_an_unfinished_stage_is_a_question_mark_not_a_zero():
    line = format_timings(4.0, None, None, chars=0)
    assert "transcribe=? inject=?" in line


def test_the_timings_measure_the_two_waits_separately():
    ticks = iter([100.0, 103.5, 103.9])
    timings = DictationTimings(4.0, clock=lambda: next(ticks))
    timings.transcript_ready(chars=57)
    timings.injected()
    assert timings.as_line() == (
        "dictation timings: audio=4.0s transcribe=3.5s inject=0.4s chars=57"
    )


def test_the_timing_line_never_carries_the_transcript():
    ticks = iter([0.0, 1.0, 1.2])
    timings = DictationTimings(2.0, clock=lambda: next(ticks))
    timings.transcript_ready(chars=len("совершенно секретный текст"))
    timings.injected()
    assert "секретный" not in timings.as_line()
    assert "chars=26" in timings.as_line()
