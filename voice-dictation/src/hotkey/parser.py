"""Hotkey string <-> virtual key codes. Pure Python, testable on any OS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Virtual key codes (winuser.h). Kept here so the parser has no WinAPI import.
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_DELETE = 0x2E

MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "lctrl": "lctrl",
    "rctrl": "rctrl",
    "alt": "alt",
    "lalt": "lalt",
    "ralt": "ralt",
    "altgr": "ralt",
    "shift": "shift",
    "lshift": "lshift",
    "rshift": "rshift",
    "win": "win",
    "lwin": "lwin",
    "rwin": "rwin",
    "meta": "win",
    "super": "win",
    "cmd": "win",
}

# Which virtual key codes satisfy each modifier token.
MODIFIER_VKS: dict[str, tuple[int, ...]] = {
    "ctrl": (VK_CONTROL, VK_LCONTROL, VK_RCONTROL),
    "lctrl": (VK_LCONTROL,),
    "rctrl": (VK_RCONTROL,),
    "alt": (VK_MENU, VK_LMENU, VK_RMENU),
    "lalt": (VK_LMENU,),
    "ralt": (VK_RMENU,),
    "shift": (VK_SHIFT, VK_LSHIFT, VK_RSHIFT),
    "lshift": (VK_LSHIFT,),
    "rshift": (VK_RSHIFT,),
    "win": (VK_LWIN, VK_RWIN),
    "lwin": (VK_LWIN,),
    "rwin": (VK_RWIN,),
}

# Canonical modifier family, used when normalising a captured combination.
MODIFIER_FAMILY: dict[str, str] = {
    "ctrl": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl",
    "alt": "alt", "lalt": "alt", "ralt": "alt",
    "shift": "shift", "lshift": "shift", "rshift": "shift",
    "win": "win", "lwin": "win", "rwin": "win",
}

NAMED_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "clear": 0x0C,
    "enter": 0x0D,
    "return": 0x0D,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "numlock": 0x90,
    "scrolllock": 0x91,
    ";": 0xBA,
    "=": 0xBB,
    "plus": 0xBB,  # the format splits on "+", so the key needs a name
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}
for _i in range(1, 25):  # F1..F24
    NAMED_KEYS[f"f{_i}"] = 0x6F + _i
for _i in range(10):  # numpad
    NAMED_KEYS[f"num{_i}"] = 0x60 + _i

# Reverse map for turning a captured virtual key back into a token.
_VK_TO_NAME: dict[int, str] = {}
for _name, _vk in NAMED_KEYS.items():
    _VK_TO_NAME.setdefault(_vk, _name)
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_TO_NAME.setdefault(ord(_c.upper()), _c)
for _c in "0123456789":
    _VK_TO_NAME.setdefault(ord(_c), _c)

# Single keys usable without any modifier.
STANDALONE_KEYS = {f"f{i}" for i in range(1, 25)}

# Keys that must not be the main key: held down for dictation they toggle a
# keyboard lock or fire a system action, and the capture widget never
# produces them either.
LOCK_KEYS = {"capslock", "numlock", "scrolllock"}
UNUSABLE_MAIN_KEYS = LOCK_KEYS | {"printscreen"}


class HotkeyError(ValueError):
    """Raised with a user-facing Russian message."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: tuple[str, ...]
    key: str
    key_vk: int

    def to_string(self) -> str:
        return "+".join(list(self.modifiers) + [self.key])

    def modifier_vks(self) -> list[tuple[int, ...]]:
        return [MODIFIER_VKS[name] for name in self.modifiers]

    def all_modifier_vks(self) -> set[int]:
        vks: set[int] = set()
        for name in self.modifiers:
            vks.update(MODIFIER_VKS[name])
        return vks


def _sort_modifiers(names: Iterable[str]) -> tuple[str, ...]:
    unique = list(dict.fromkeys(names))
    return tuple(
        sorted(unique, key=lambda name: MODIFIER_ORDER.index(MODIFIER_FAMILY[name]))
    )


def key_token_to_vk(token: str) -> int | None:
    token = token.strip().lower()
    if not token:
        return None
    if token in NAMED_KEYS:
        return NAMED_KEYS[token]
    if len(token) == 1:
        if token.isascii() and token.isalpha():
            return ord(token.upper())
        if token.isascii() and token.isdigit():
            return ord(token)
    return None


def vk_to_key_token(vk: int) -> str | None:
    return _VK_TO_NAME.get(vk)


def parse(text: str) -> Hotkey:
    """"ctrl+alt+space" -> Hotkey. Raises HotkeyError with a Russian message."""
    if not text or not text.strip():
        raise HotkeyError("Горячая клавиша не задана")
    tokens = [part.strip().lower() for part in text.split("+")]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise HotkeyError("Горячая клавиша не задана")

    modifiers: list[str] = []
    key_token: str | None = None
    for token in tokens:
        if token in MODIFIER_ALIASES:
            modifiers.append(MODIFIER_ALIASES[token])
            continue
        if key_token is not None:
            raise HotkeyError("В комбинации может быть только одна обычная клавиша")
        key_token = token

    if key_token is None:
        raise HotkeyError(
            "В комбинации нет обычной клавиши "
            "(клавишу «+» записывайте словом plus)"
        )

    vk = key_token_to_vk(key_token)
    if vk is None:
        raise HotkeyError(f"Неизвестная клавиша: {key_token}")

    if not modifiers and key_token not in STANDALONE_KEYS:
        raise HotkeyError("Нужен хотя бы один модификатор (Ctrl, Alt, Shift или Win)")

    hotkey = Hotkey(modifiers=_sort_modifiers(modifiers), key=key_token, key_vk=vk)
    _reject_unusable_key(hotkey)
    _reject_system_combinations(hotkey)
    return hotkey


def _reject_unusable_key(hotkey: Hotkey) -> None:
    if hotkey.key in LOCK_KEYS:
        raise HotkeyError(
            f"Клавиша {hotkey.key} не подходит для диктовки — "
            "её удержание переключает режим клавиатуры"
        )
    if hotkey.key in UNUSABLE_MAIN_KEYS:
        raise HotkeyError(
            f"Клавиша {hotkey.key} не подходит для диктовки — "
            "её обрабатывает сама Windows"
        )


def _reject_system_combinations(hotkey: Hotkey) -> None:
    families = {MODIFIER_FAMILY[name] for name in hotkey.modifiers}
    if families == {"ctrl", "alt"} and hotkey.key_vk == VK_DELETE:
        raise HotkeyError(
            "Ctrl+Alt+Del перехватить нельзя — эту комбинацию обрабатывает сама Windows"
        )
    if families == {"win"} and hotkey.key == "l":
        raise HotkeyError(
            "Win+L перехватить нельзя — эту комбинацию обрабатывает сама Windows"
        )


def warnings_for(hotkey: Hotkey) -> list[str]:
    families = {MODIFIER_FAMILY[name] for name in hotkey.modifiers}
    if families == {"alt"} and hotkey.key == "space":
        return [
            "Alt+Space используется Windows для системного меню окна — "
            "возможны конфликты"
        ]
    return []


def from_parts(modifier_names: Iterable[str], key_vk: int) -> Hotkey:
    """Build a hotkey from what the capture widget observed."""
    token = vk_to_key_token(key_vk)
    if token is None:
        raise HotkeyError("Эта клавиша не поддерживается")
    normalised = [MODIFIER_ALIASES[name] for name in modifier_names if name in MODIFIER_ALIASES]
    if not normalised and token not in STANDALONE_KEYS:
        raise HotkeyError("Нужен хотя бы один модификатор (Ctrl, Alt, Shift или Win)")
    hotkey = Hotkey(modifiers=_sort_modifiers(normalised), key=token, key_vk=key_vk)
    _reject_unusable_key(hotkey)
    _reject_system_combinations(hotkey)
    return hotkey
