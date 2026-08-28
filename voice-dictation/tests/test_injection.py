import sys

import pytest

from src.injection import focus, typer
from src.utils import single_instance


# ------------------------------------------------------------ newlines

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("одна\nдве", "одна\r\nдве"),
        ("одна\r\nдве", "одна\r\nдве"),
        ("старый\rмак", "старый\r\nмак"),
        ("смешанный\r\nи\nи\r", "смешанный\r\nи\r\nи\r\n"),
        ("без переводов строки", "без переводов строки"),
        ("", ""),
    ],
)
def test_normalize_newlines(raw, expected):
    assert typer.normalize_newlines(raw) == expected


def test_normalize_newlines_is_idempotent():
    once = typer.normalize_newlines("a\nb\rc\r\nd")
    assert typer.normalize_newlines(once) == once


# ------------------------------------------------------- text expansion

def test_bmp_text_becomes_one_event_per_character():
    assert typer.text_to_key_events("Да") == [
        (typer.EVENT_UNICODE, ord("Д")),
        (typer.EVENT_UNICODE, ord("а")),
    ]


def test_astral_character_becomes_surrogate_pair():
    events = typer.text_to_key_events("\U0001F600")
    assert events == [
        (typer.EVENT_UNICODE, 0xD83D),
        (typer.EVENT_UNICODE, 0xDE00),
    ]
    high, low = events[0][1], events[1][1]
    assert 0xD800 <= high <= 0xDBFF
    assert 0xDC00 <= low <= 0xDFFF


def test_newline_becomes_a_single_return_key_not_a_unicode_char():
    events = typer.text_to_key_events("a\nb")
    assert events == [
        (typer.EVENT_UNICODE, ord("a")),
        (typer.EVENT_VK, typer.VK_RETURN),
        (typer.EVENT_UNICODE, ord("b")),
    ]
    assert all(code != 0x0A for kind, code in events if kind == typer.EVENT_UNICODE)


def test_crlf_does_not_produce_two_line_breaks():
    assert typer.text_to_key_events("a\r\nb\rc").count((typer.EVENT_VK, typer.VK_RETURN)) == 2


def test_events_round_trip_back_to_the_normalised_text():
    text = "строка\nвторая\U0001F600"
    units = []
    for kind, code in typer.text_to_key_events(text):
        units.extend([0x0D, 0x0A] if kind == typer.EVENT_VK else [code])
    restored = b"".join(unit.to_bytes(2, "little") for unit in units).decode("utf-16-le")
    assert restored == typer.normalize_newlines(text)


# ------------------------------------------------------ modifier release

def _plan(held):
    held = set(held)
    return typer.modifier_release_plan(lambda vk: vk in held)


def test_nothing_held_means_no_events():
    assert _plan([]) == []


def test_right_side_modifiers_are_released_by_their_own_vk():
    assert _plan([typer.VK_RCONTROL]) == [(typer.VK_RCONTROL, True)]
    assert _plan([typer.VK_RSHIFT]) == [(typer.VK_RSHIFT, True)]
    # Right Alt is the one exception: on its own it needs the neutralising tap.
    assert _plan([typer.VK_RMENU])[-1] == (typer.VK_RMENU, True)


def test_alt_is_released_before_ctrl():
    plan = _plan([typer.VK_LCONTROL, typer.VK_LMENU])
    releases = [vk for vk, key_up in plan if key_up]
    # An Alt-up while Ctrl is still down cannot be read as menu activation.
    assert releases.index(typer.VK_LMENU) < releases.index(typer.VK_LCONTROL)


def test_alt_with_ctrl_held_needs_no_neutralising_tap():
    plan = _plan([typer.VK_LCONTROL, typer.VK_LMENU])
    assert plan == [(typer.VK_LMENU, True), (typer.VK_LCONTROL, True)]


@pytest.mark.parametrize("alt", [typer.VK_LMENU, typer.VK_RMENU])
def test_lone_alt_release_is_preceded_by_a_neutralising_tap(alt):
    plan = _plan([alt])
    assert plan == [
        (typer.VK_CONTROL, False),
        (typer.VK_CONTROL, True),
        (alt, True),
    ]
    # The tap must happen while Alt is still down: a bare Alt-up activates the
    # focused window's menu bar and the text field loses focus.
    assert plan.index((typer.VK_CONTROL, True)) < plan.index((alt, True))


def test_alt_with_shift_only_still_gets_the_tap():
    plan = _plan([typer.VK_LMENU, typer.VK_LSHIFT])
    assert plan[:2] == [(typer.VK_CONTROL, False), (typer.VK_CONTROL, True)]


def test_both_sides_are_released_when_both_are_held():
    plan = _plan([typer.VK_LCONTROL, typer.VK_RCONTROL])
    assert plan == [(typer.VK_LCONTROL, True), (typer.VK_RCONTROL, True)]


def test_generic_modifier_vks_are_never_used_for_release():
    plan = _plan(typer.SIDED_MODIFIERS)
    assert [vk for vk, _ in plan] == list(typer.SIDED_MODIFIERS)
    assert typer.VK_CONTROL not in [vk for vk, _ in plan]
    assert typer.VK_MENU not in [vk for vk, _ in plan]
    assert typer.VK_SHIFT not in [vk for vk, _ in plan]


def test_win_key_release_is_preceded_by_a_neutralising_tap():
    plan = _plan([typer.VK_LWIN])
    assert plan == [
        (typer.VK_CONTROL, False),
        (typer.VK_CONTROL, True),
        (typer.VK_LWIN, True),
    ]
    # The tap must happen while Win is still down, otherwise the Start menu opens.
    assert plan.index((typer.VK_CONTROL, True)) < plan.index((typer.VK_LWIN, True))


def test_both_win_keys_are_released_after_one_tap():
    plan = _plan([typer.VK_LWIN, typer.VK_RWIN])
    assert plan.count((typer.VK_CONTROL, False)) == 1
    assert (typer.VK_LWIN, True) in plan and (typer.VK_RWIN, True) in plan


def test_extended_keys_cover_the_right_side_only():
    assert typer.EXTENDED_KEYS == {typer.VK_RCONTROL, typer.VK_RMENU, typer.VK_RWIN}
    assert typer.VK_LCONTROL not in typer.EXTENDED_KEYS


# ------------------------------------------------------ non-Windows guards
# The suite runs on Linux for development and on windows-latest in CI before
# the .exe is built, so anything that asserts the shape of the non-Windows
# fallbacks is skipped on Windows instead of failing the build.

on_windows = sys.platform == "win32"
skip_on_windows = pytest.mark.skipif(on_windows, reason="проверяет запасной путь вне Windows")


@skip_on_windows
def test_typer_knows_it_is_not_on_windows():
    assert typer._IS_WINDOWS is False


def test_typer_platform_flag_matches_the_platform():
    assert typer._IS_WINDOWS is on_windows


def test_release_stuck_modifiers_is_a_no_op():
    # On Windows this really asks GetAsyncKeyState and releases nothing extra;
    # either way the call must be silent and return nothing.
    assert typer.release_stuck_modifiers() is None


@skip_on_windows
def test_clipboard_helpers_are_inert():
    assert typer.get_clipboard_text() is None
    assert typer.set_clipboard_text("текст") is None


@skip_on_windows
@pytest.mark.parametrize("call", [typer.paste_via_clipboard, typer.type_via_sendinput])
def test_typing_refuses_outside_windows(call):
    with pytest.raises(typer.InjectionError):
        call("текст")


@skip_on_windows
def test_insert_text_does_not_loop_forever_outside_windows():
    with pytest.raises(typer.InjectionError):
        typer.insert_text("текст")


@skip_on_windows
def test_focus_helpers_are_inert():
    assert focus.get_foreground_window() == 0
    assert focus.is_window(123) is False
    assert focus.belongs_to_this_process(123) is False
    assert focus.is_hung(123) is False
    assert focus.restore_foreground(123) is False


def test_focus_helpers_answer_without_raising():
    # Real handles on Windows, zeroes elsewhere; nothing here may throw.
    assert isinstance(focus.get_foreground_window(), int)
    assert focus.is_window(123) in (True, False)
    assert focus.belongs_to_this_process(123) in (True, False)
    assert focus.is_hung(123) in (True, False)
    assert focus.restore_foreground(123) in (True, False)


def test_single_instance_allows_startup():
    guard = single_instance.SingleInstance()
    assert guard.acquire() is True
    assert guard.already_running is False
    guard.release()


@skip_on_windows
def test_signalling_the_running_copy_is_inert_outside_windows():
    # Not run on Windows: a failed broadcast ends in a modal message box, which
    # would hang the build.
    assert single_instance.signal_existing_instance() is None


@skip_on_windows
def test_show_settings_message_id_is_zero_outside_windows():
    assert single_instance.show_settings_message_id() == 0


def test_show_settings_message_id_is_registered_on_windows():
    message_id = single_instance.show_settings_message_id()
    # RegisterWindowMessage hands out ids in the 0xC000-0xFFFF range.
    assert message_id > 0 if on_windows else message_id == 0


# --------------------------------------------------------------- constants

def test_mutex_lives_in_the_local_namespace():
    assert single_instance.MUTEX_NAME.startswith("Local\\")


# ------------------------------------------------------- clipboard paste

@pytest.fixture
def pretend_windows(monkeypatch):
    """paste_via_clipboard on a pretend Windows: records what it does, in order.

    Everything below set_clipboard_text is stubbed, so the fixture behaves the
    same on Windows and on Linux.
    """
    events = []
    monkeypatch.setattr(typer, "_IS_WINDOWS", True)
    monkeypatch.setattr(typer, "_write_clipboard", lambda text: events.append(("write", text)))
    monkeypatch.setattr(typer, "release_stuck_modifiers", lambda: None)
    monkeypatch.setattr(
        typer,
        "wait_for_clipboard",
        lambda expected, read: events.append(("settle", expected)) or True,
    )
    monkeypatch.setattr(typer, "_vk_input", lambda vk, key_up: (vk, key_up), raising=False)
    monkeypatch.setattr(typer, "_send", lambda inputs: events.append(("paste", inputs)),
                        raising=False)
    monkeypatch.setattr(typer.time, "sleep", lambda delay: events.append(("sleep", delay)))
    return events


def test_transcript_is_normalised_before_it_is_pasted(monkeypatch, pretend_windows):
    monkeypatch.setattr(typer, "get_clipboard_text", lambda: None)
    typer.paste_via_clipboard("одна\nдве")
    assert pretend_windows[0] == ("write", "одна\r\nдве")


def test_restored_clipboard_keeps_the_users_own_line_endings(monkeypatch, pretend_windows):
    # Code, a shell snippet or JSON copied by the user is data the app does not
    # own: dictating must not turn its bare U+000A into CR LF.
    original = '{"a": 1,\n "b": 2}\n'
    monkeypatch.setattr(typer, "get_clipboard_text", lambda: original)
    typer.paste_via_clipboard("текст")
    writes = [text for kind, text in pretend_windows if kind == "write"]
    assert writes[-1] == original


def test_clipboard_is_restored_only_after_the_paste_had_time_to_land(
    monkeypatch, pretend_windows
):
    monkeypatch.setattr(typer, "get_clipboard_text", lambda: "старое")
    typer.paste_via_clipboard("текст")
    kinds = [kind for kind, _ in pretend_windows]
    assert kinds == ["write", "settle", "paste", "sleep", "write"]
    delay = pretend_windows[3][1]
    # Restoring before the target application has pasted means it pastes the
    # old text instead of the transcript.
    assert delay >= 1.0


def test_clipboard_is_restored_even_when_the_paste_fails(monkeypatch, pretend_windows):
    monkeypatch.setattr(typer, "get_clipboard_text", lambda: "старое")

    def boom(inputs):
        raise typer.InjectionError("нет")

    monkeypatch.setattr(typer, "_send", boom, raising=False)
    with pytest.raises(typer.InjectionError):
        typer.paste_via_clipboard("текст")
    # No point waiting when nothing was pasted, but the content must come back.
    assert [kind for kind, _ in pretend_windows] == ["write", "settle", "write"]
    assert pretend_windows[-1] == ("write", "старое")


# ------------------------------------------------------- clipboard settle

class FakeClipboard:
    """Clipboard that only starts reading back the new text after N reads.

    Stands in for the real race: SetClipboardData returned, but the target
    application cannot read the new content back yet.
    """

    def __init__(self, ready_after: int = 0, content: str = "старое") -> None:
        self._ready_after = ready_after
        self._content = content
        self.reads = 0
        self.written = None

    def write(self, text: str) -> None:
        self.written = text

    def read(self) -> str | None:
        self.reads += 1
        if self.written is not None and self.reads > self._ready_after:
            return self.written
        return self._content


class FakeClock:
    """Monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = []

    def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.now += delay

    def __call__(self) -> float:
        return self.now


def _settle(clipboard, clock, timeout=typer.CLIPBOARD_SETTLE_TIMEOUT):
    return typer.wait_for_clipboard(
        clipboard.written,
        clipboard.read,
        sleep=clock.sleep,
        clock=clock,
        timeout=timeout,
    )


def test_settle_waits_a_beat_even_when_the_clipboard_reads_back_at_once():
    clipboard = FakeClipboard(ready_after=0)
    clipboard.write("текст")
    clock = FakeClock()
    assert _settle(clipboard, clock) is True
    assert clipboard.reads == 1
    assert clock.slept == [typer.CLIPBOARD_SETTLE_FLOOR]


def test_settle_retries_until_the_write_reads_back():
    clipboard = FakeClipboard(ready_after=3)
    clipboard.write("текст")
    clock = FakeClock()
    assert _settle(clipboard, clock) is True
    assert clipboard.reads == 4  # three misses, then the match
    assert len(clock.slept) == 4  # the floor plus one poll per miss
    assert clock.now < typer.CLIPBOARD_SETTLE_TIMEOUT


def test_settle_gives_up_at_the_cap_and_lets_the_paste_go_ahead():
    clipboard = FakeClipboard(ready_after=10_000)  # never reads back
    clipboard.write("текст")
    clock = FakeClock()
    assert _settle(clipboard, clock) is False
    # Capped, not endless: the user gets a paste attempt either way.
    cap = (
        typer.CLIPBOARD_SETTLE_FLOOR
        + typer.CLIPBOARD_SETTLE_TIMEOUT
        + typer.CLIPBOARD_SETTLE_POLL
    )
    assert clock.now <= cap
    assert clipboard.reads < 100


def test_settle_survives_a_clipboard_that_cannot_be_read():
    def boom():
        raise typer.InjectionError("занят")

    clock = FakeClock()
    assert typer.wait_for_clipboard(
        "текст", boom, sleep=clock.sleep, clock=clock, timeout=0.05
    ) is False


def test_paste_settles_on_the_normalised_text_before_pressing_ctrl_v(
    monkeypatch, pretend_windows
):
    monkeypatch.setattr(typer, "get_clipboard_text", lambda: None)
    typer.paste_via_clipboard("одна\nдве")
    kinds = [kind for kind, _ in pretend_windows]
    assert kinds.index("settle") < kinds.index("paste")
    settled = [value for kind, value in pretend_windows if kind == "settle"]
    assert settled == ["одна\r\nдве"]
