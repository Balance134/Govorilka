import pytest

from src.hotkey.parser import (
    FAMILY_SIDE_VKS,
    LOCK_KEYS,
    SYSTEM_KEYS,
    VK_LCONTROL,
    VK_RCONTROL,
    HotkeyError,
    from_parts,
    key_token_to_vk,
    parse,
    resolve_modifier_side,
    side_required_message,
    vk_to_key_token,
    collapse_altgr,
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
    with pytest.raises(HotkeyError, match="обрабатывает сама Windows"):
        parse("ctrl+printscreen")


def test_each_unusable_key_gets_its_own_reason():
    """The lock branch used to swallow the system branch entirely."""
    with pytest.raises(HotkeyError, match="режим клавиатуры"):
        parse("ctrl+capslock")
    with pytest.raises(HotkeyError, match="обрабатывает сама Windows"):
        parse("ctrl+printscreen")
    assert LOCK_KEYS.isdisjoint(SYSTEM_KEYS)


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


# ------------------------------------- a single side-specific modifier hotkey
@pytest.mark.parametrize(
    "text", ["rctrl", "lctrl", "ralt", "lalt", "rshift", "lshift"]
)
def test_a_single_side_specific_modifier_is_a_hotkey(text):
    hotkey = parse(text)
    assert hotkey.is_modifier_only
    assert hotkey.key == ""
    assert hotkey.to_string() == text
    assert parse(hotkey.to_string()) == hotkey


def test_modifier_only_hotkey_triggers_on_its_own_vk():
    assert parse("rctrl").trigger_vk == VK_RCONTROL
    assert parse("ctrl+alt+space").trigger_vk == 0x20


@pytest.mark.parametrize("text", ["lwin", "rwin"])
def test_the_windows_key_alone_is_refused(text):
    """Releasing it opens the Start menu, which takes the focus mid-dictation."""
    with pytest.raises(HotkeyError, match="меню «Пуск»"):
        parse(text)
    with pytest.raises(HotkeyError, match="меню «Пуск»"):
        from_parts([text])


def test_a_single_alt_is_allowed_but_warned_about():
    assert "AltGr" in warnings_for(parse("ralt"))[0]
    assert "меню окна" in warnings_for(parse("lalt"))[0]
    assert warnings_for(parse("rctrl")) == []


def test_altgr_chord_is_one_key():
    """AltGr sends a synthetic left Ctrl together with the right Alt."""
    assert collapse_altgr(["lctrl", "ralt"]) == ["ralt"]
    assert collapse_altgr(["ralt", "lctrl"]) == ["ralt"]
    assert collapse_altgr(["lctrl", "lalt"]) == ["lctrl", "lalt"]
    assert collapse_altgr(["rctrl"]) == ["rctrl"]


def test_altgr_is_the_right_alt():
    assert parse("altgr").to_string() == "ralt"


@pytest.mark.parametrize(
    "text,sides",
    [
        ("ctrl", "rctrl или lctrl"),
        ("alt", "ralt или lalt"),
        ("shift", "rshift или lshift"),
        ("win", "rwin или lwin"),
    ],
)
def test_a_generic_modifier_alone_asks_for_a_side(text, sides):
    with pytest.raises(HotkeyError, match=sides):
        parse(text)


def test_two_modifiers_without_a_key_are_still_rejected():
    with pytest.raises(HotkeyError, match="обычной клавиши"):
        parse("rctrl+ralt")


def test_from_parts_builds_a_modifier_only_hotkey():
    assert from_parts(["rctrl"]).to_string() == "rctrl"
    with pytest.raises(HotkeyError, match="rctrl или lctrl"):
        from_parts(["ctrl"])


@pytest.mark.parametrize(
    "text",
    [
        "ctrl+alt+delete",
        "ctrl+alt+shift+delete",
        "ctrl+alt+win+del",
        "win+l",
        "win+shift+l",
        "ctrl+win+alt+l",
    ],
)
def test_windows_reserved_combinations_are_rejected_with_extra_modifiers(text):
    with pytest.raises(HotkeyError, match="Windows"):
        parse(text)


def test_a_reserved_key_stays_usable_in_a_different_combination():
    assert parse("ctrl+shift+delete").to_string() == "ctrl+shift+delete"
    assert parse("ctrl+shift+l").to_string() == "ctrl+shift+l"


# ------------------------------------ which side of the keyboard was pressed
def test_key_state_alone_names_the_side():
    # Windows reports the generic VK_CONTROL and no usable scan code.
    assert resolve_modifier_side(0x11, 0, ["rctrl"]) == "rctrl"


def test_key_state_outranks_a_disagreeing_event():
    assert resolve_modifier_side(VK_LCONTROL, 0x1D, ["rctrl"]) == "rctrl"


def test_native_virtual_key_is_used_when_windows_cannot_be_asked():
    assert resolve_modifier_side(VK_RCONTROL, 0x1D, []) == "rctrl"


def test_scancode_is_the_last_resort():
    assert resolve_modifier_side(0x11, 0xE01D, []) == "rctrl"
    assert resolve_modifier_side(0x11, 0x1D, []) == "lctrl"
    assert resolve_modifier_side(0x12, 0x138, []) == "ralt"  # 0x100 extended flag
    assert resolve_modifier_side(0x10, 0x36, []) == "rshift"
    assert resolve_modifier_side(0x10, 0x2A, []) == "lshift"


def test_both_sides_held_falls_back_to_the_event():
    assert resolve_modifier_side(0x11, 0xE01D, ["lctrl", "rctrl"]) == "rctrl"
    assert resolve_modifier_side(VK_LCONTROL, 0, ["lctrl", "rctrl"]) == "lctrl"


def test_both_sides_held_and_nothing_else_known_is_ambiguous():
    assert resolve_modifier_side(0x11, 0, ["lctrl", "rctrl"]) is None


def test_everything_ambiguous_gives_no_side():
    assert resolve_modifier_side(0x11, 0, []) is None
    assert resolve_modifier_side(0, 0, []) is None


def test_an_ambiguous_side_is_refused_out_loud():
    message = side_required_message("ctrl")
    assert "rctrl" in message and "lctrl" in message
    with pytest.raises(HotkeyError, match="rctrl или lctrl"):
        parse("ctrl")


def test_every_family_can_be_probed_on_both_sides():
    assert set(FAMILY_SIDE_VKS) == {"ctrl", "alt", "shift", "win"}
    for family, sides in FAMILY_SIDE_VKS.items():
        names = [name for name, _vk in sides]
        # Combined with a key, because the Windows key alone is refused.
        assert all(parse(f"{name}+f9").modifiers == (name,) for name in names)
