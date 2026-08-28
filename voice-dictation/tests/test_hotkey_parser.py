import pytest

from src.hotkey.parser import (
    VK_LCONTROL,
    HotkeyError,
    from_parts,
    key_token_to_vk,
    parse,
    vk_to_key_token,
    warnings_for,
)


def test_default_combination():
    hotkey = parse("ctrl+alt+space")
    assert hotkey.modifiers == ("ctrl", "alt")
    assert hotkey.key == "space"
    assert hotkey.key_vk == 0x20


def test_string_round_trip_is_symmetric():
    for text in ("ctrl+alt+space", "ctrl+shift+f9", "alt+win+q", "f13"):
        assert parse(text).to_string() == text


def test_order_is_normalised_and_case_ignored():
    assert parse("ALT+Ctrl+Space").to_string() == "ctrl+alt+space"


def test_aliases():
    assert parse("control+shift+a").to_string() == "ctrl+shift+a"
    assert parse("meta+z").to_string() == "win+z"


def test_function_key_needs_no_modifier():
    assert parse("f5").to_string() == "f5"


def test_plain_key_without_modifier_is_rejected():
    with pytest.raises(HotkeyError, match="модификатор"):
        parse("space")


def test_missing_key_is_rejected():
    with pytest.raises(HotkeyError, match="обычной клавиши"):
        parse("ctrl+alt")


def test_two_regular_keys_are_rejected():
    with pytest.raises(HotkeyError, match="одна обычная клавиша"):
        parse("ctrl+a+b")


def test_empty_is_rejected():
    with pytest.raises(HotkeyError):
        parse("   ")


def test_unknown_key_is_rejected():
    with pytest.raises(HotkeyError, match="Неизвестная клавиша"):
        parse("ctrl+щщ")


@pytest.mark.parametrize("text", ["ctrl+alt+delete", "win+l"])
def test_system_combinations_are_rejected(text):
    with pytest.raises(HotkeyError, match="Windows"):
        parse(text)


def test_alt_space_is_allowed_but_warns():
    hotkey = parse("alt+space")
    assert warnings_for(hotkey)
    assert not warnings_for(parse("ctrl+alt+space"))


def test_side_specific_modifiers():
    hotkey = parse("lctrl+space")
    assert hotkey.modifiers == ("lctrl",)
    assert hotkey.all_modifier_vks() == {VK_LCONTROL}


def test_vk_helpers():
    assert key_token_to_vk("space") == 0x20
    assert key_token_to_vk("a") == 0x41
    assert key_token_to_vk("7") == 0x37
    assert key_token_to_vk("nope") is None
    assert vk_to_key_token(0x20) == "space"
    assert vk_to_key_token(0x41) == "a"


def test_from_parts_matches_parse():
    assert from_parts(["ctrl", "alt"], 0x20).to_string() == "ctrl+alt+space"
    with pytest.raises(HotkeyError):
        from_parts([], 0x20)


@pytest.mark.parametrize("text", ["ctrl+capslock", "ctrl+numlock", "alt+scrolllock"])
def test_lock_keys_are_rejected_as_the_main_key(text):
    with pytest.raises(HotkeyError, match="режим клавиатуры"):
        parse(text)


def test_printscreen_is_rejected_as_the_main_key():
    with pytest.raises(HotkeyError, match="Windows"):
        parse("ctrl+printscreen")


def test_lock_keys_are_rejected_from_the_capture_widget_too():
    with pytest.raises(HotkeyError):
        from_parts(["ctrl"], 0x14)


def test_lock_keys_stay_usable_as_modifier_free_names():
    # The token itself is still known, only its use as the main key is barred.
    assert key_token_to_vk("capslock") == 0x14


def test_plus_key_is_expressible_by_name():
    hotkey = parse("ctrl+plus")
    assert hotkey.key_vk == 0xBB


def test_missing_key_error_mentions_the_plus_workaround():
    with pytest.raises(HotkeyError, match="plus"):
        parse("ctrl++")


def test_equals_sign_stays_the_canonical_name_for_that_key():
    assert vk_to_key_token(0xBB) == "="
