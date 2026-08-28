"""Configuration dataclasses and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_HOTKEY = "ctrl+alt+space"
DEFAULT_PASTE_MODE = "clipboard"
PASTE_MODES = ("clipboard", "sendinput")

DEFAULT_VOCABULARY: list[str] = [
    "n8n",
    "Supabase",
    "pgvector",
    "Vercel",
    "Tailwind",
    "AutoLISP",
    "ComfyUI",
    "LUNEO",
    "Claude Code",
]

DEFAULT_REPLACEMENTS: list[dict[str, Any]] = [
    {"to": "Claude Code", "from": ["клод код", "клоткот", "клод коуд", "клауд код"]},
    {"to": "n8n", "from": ["эн восемь эн", "н8н"]},
]


@dataclass
class ReplacementRule:
    """One line of the replacement table: target form and heard variants."""

    to: str
    variants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"to": self.to, "from": list(self.variants)}

    @staticmethod
    def from_dict(raw: Any) -> "ReplacementRule | None":
        if not isinstance(raw, dict):
            return None
        to = raw.get("to")
        if not isinstance(to, str) or not to.strip():
            return None
        variants_raw = raw.get("from")
        variants: list[str] = []
        if isinstance(variants_raw, list):
            for item in variants_raw:
                if isinstance(item, str) and item.strip():
                    variants.append(item.strip())
        if not variants:
            # A rule with no variants can never fire and the settings text box
            # cannot show it, so the two layers agree to drop it here.
            return None
        return ReplacementRule(to=to.strip(), variants=variants)


@dataclass
class AppConfig:
    api_key: str = ""
    vocabulary: list[str] = field(default_factory=lambda: list(DEFAULT_VOCABULARY))
    replacements: list[ReplacementRule] = field(
        default_factory=lambda: [
            r
            for r in (ReplacementRule.from_dict(d) for d in DEFAULT_REPLACEMENTS)
            if r is not None
        ]
    )
    hotkey: str = DEFAULT_HOTKEY
    language_codes: list[str] = field(default_factory=list)
    paste_mode: str = DEFAULT_PASTE_MODE
    # Set by store.load() when a broken config.json was discarded; never saved.
    was_reset: bool = field(default=False, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key,
            "vocabulary": list(self.vocabulary),
            "replacements": [r.to_dict() for r in self.replacements],
            "hotkey": self.hotkey,
            "language_codes": list(self.language_codes),
            "paste_mode": self.paste_mode,
        }

    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())
