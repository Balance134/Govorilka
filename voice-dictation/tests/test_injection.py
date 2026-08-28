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
    assert _plan([typer.VK_RMENU]) == [(typer.VK_RMENU, True)]
    assert _plan([typer.VK_RSHIFT]) == [(typer.VK_RSHIFT, True)]


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

def test_typer_knows_it_is_not_on_windows():
    assert typer._IS_WINDOWS is False


def test_release_stuck_modifiers_is_a_no_op():
    assert typer.release_stuck_modifiers() is None


def test_clipboard_helpers_are_inert():
    assert typer.get_clipboard_text() is None
    assert typer.set_clipboard_text("текст") is None


@pytest.mark.parametrize("call", [typer.paste_via_clipboard, typer.type_via_sendinput])
def test_typing_refuses_outside_windows(call):
    with pytest.raises(typer.InjectionError):
        call("текст")


def test_insert_text_does_not_loop_forever_outside_windows():
    with pytest.raises(typer.InjectionError):
        typer.insert_text("текст")


def test_focus_helpers_are_inert():
    assert focus.get_foreground_window() == 0
    assert focus.is_window(123) is False
    assert focus.belongs_to_this_process(123) is False
    assert focus.is_hung(123) is False
    assert focus.restore_foreground(123) is False


def test_single_instance_allows_startup_outside_windows():
    guard = single_instance.SingleInstance()
    assert guard.acquire() is True
    assert guard.already_running is False
    guard.release()
    assert single_instance.show_settings_message_id() == 0
    assert single_instance.signal_existing_instance() is None


# --------------------------------------------------------------- constants

def test_mutex_lives_in_the_local_namespace():
    assert single_instance.MUTEX_NAME.startswith("Local\\")


def test_clipboard_restore_delay_leaves_slow_apps_time_to_paste():
    assert typer.CLIPBOARD_RESTORE_DELAY >= 1.0
