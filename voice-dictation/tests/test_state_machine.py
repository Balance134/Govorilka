import pytest

from src.utils.state import STATE_TITLES, AppState, StateMachine


def test_starts_idle():
    assert StateMachine().state is AppState.IDLE


def test_happy_path():
    seen = []
    machine = StateMachine(on_change=seen.append)
    assert machine.to(AppState.RECORDING)
    assert machine.to(AppState.PROCESSING)
    assert machine.to(AppState.TYPING)
    assert machine.to(AppState.IDLE)
    assert seen == [
        AppState.RECORDING, AppState.PROCESSING, AppState.TYPING, AppState.IDLE
    ]


def test_second_press_during_processing_is_rejected():
    machine = StateMachine()
    machine.to(AppState.RECORDING)
    machine.to(AppState.PROCESSING)
    assert machine.is_busy()
    assert not machine.to(AppState.RECORDING)
    assert machine.state is AppState.PROCESSING


def test_recording_counts_as_busy():
    """A second press during recording must be rejected, not restart it."""
    machine = StateMachine()
    assert machine.to(AppState.RECORDING)
    assert machine.is_busy()
    assert not machine.to(AppState.RECORDING)
    assert machine.state is AppState.RECORDING


def test_idle_and_error_are_not_busy():
    machine = StateMachine()
    assert not machine.is_busy()
    machine.to(AppState.RECORDING)
    machine.to(AppState.ERROR)
    assert not machine.is_busy()


def test_force_idle_when_already_idle_is_silent():
    seen = []
    machine = StateMachine(on_change=seen.append)
    machine.force_idle()
    assert seen == []


def test_watchdog_can_free_a_stuck_processing_state():
    machine = StateMachine()
    machine.to(AppState.RECORDING)
    machine.to(AppState.PROCESSING)
    machine.force_idle()  # what the busy watchdog does
    assert not machine.is_busy()
    assert machine.to(AppState.RECORDING)


def test_too_short_recording_returns_to_idle():
    machine = StateMachine()
    machine.to(AppState.RECORDING)
    assert machine.to(AppState.IDLE)
    assert machine.state is AppState.IDLE


def test_error_is_reachable_from_every_working_state():
    for state in (AppState.RECORDING, AppState.PROCESSING, AppState.TYPING):
        machine = StateMachine()
        machine.to(AppState.RECORDING)
        if state is not AppState.RECORDING:
            machine.to(AppState.PROCESSING)
        if state is AppState.TYPING:
            machine.to(AppState.TYPING)
        assert machine.to(AppState.ERROR)


def test_error_recovers_to_idle_and_allows_recording():
    machine = StateMachine()
    machine.to(AppState.RECORDING)
    machine.to(AppState.ERROR)
    assert machine.to(AppState.IDLE)
    assert machine.to(AppState.RECORDING)


def test_same_state_transition_returns_false_and_notifies_nobody():
    """Callers must be able to tell "moved" from "was already there"."""
    seen = []
    machine = StateMachine(on_change=seen.append)
    assert not machine.to(AppState.IDLE)
    assert machine.state is AppState.IDLE
    assert seen == []


def test_typing_cannot_start_from_idle():
    machine = StateMachine()
    assert not machine.to(AppState.TYPING)


def test_force_idle_always_works():
    seen = []
    machine = StateMachine(on_change=seen.append)
    machine.to(AppState.RECORDING)
    machine.to(AppState.PROCESSING)
    machine.force_idle()
    assert machine.state is AppState.IDLE
    assert seen[-1] is AppState.IDLE


def test_every_state_has_a_russian_title():
    for state in AppState:
        assert STATE_TITLES[state]
