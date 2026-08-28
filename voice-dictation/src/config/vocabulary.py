"""Two-layer vocabulary: API bias list + local deterministic replacements.

Pure functions only - no Qt, no WinAPI, no network. Fully unit tested.
"""

from __future__ import annotations

import re
from typing import Iterable

from .model import ReplacementRule

VOCABULARY_HARD_LIMIT = 1000
VOCABULARY_SOFT_LIMIT = 100


class ValidationError(ValueError):
    """Raised with a user-facing Russian message."""


# --------------------------------------------------------------------------
# Layer 1: custom_vocabulary
# --------------------------------------------------------------------------

def parse_vocabulary_text(text: str) -> list[str]:
    """Comma and newline separated terms -> clean, deduplicated list.

    Case of the first occurrence wins; later differently-cased duplicates drop.
    """
    raw_terms: list[str] = []
    for line in text.splitlines():
        for chunk in line.split(","):
            term = chunk.strip()
            if term:
                raw_terms.append(term)
    return dedupe_terms(raw_terms)


def dedupe_terms(terms: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def serialize_vocabulary(terms: Iterable[str]) -> str:
    return ", ".join(terms)


def validate_vocabulary(terms: list[str]) -> list[str]:
    """Hard limit blocks saving; the soft limit only warns (see warnings())."""
    if len(terms) > VOCABULARY_HARD_LIMIT:
        raise ValidationError("Словарь содержит более 1000 терминов")
    return terms


def vocabulary_warnings(terms: list[str]) -> list[str]:
    if len(terms) > VOCABULARY_SOFT_LIMIT:
        return [
            "Рекомендуется не более 100 терминов — при большем количестве "
            "качество распознавания может падать"
        ]
    return []


# --------------------------------------------------------------------------
# Layer 2: local replacements
# --------------------------------------------------------------------------

def parse_replacements_text(text: str) -> list[ReplacementRule]:
    """One rule per line: ``target = variant, variant``.

    Blank lines and '#' comments are skipped. A line without '=' is an error
    that names its 1-based line number.
    """
    rules: list[ReplacementRule] = []
    seen_targets: dict[str, ReplacementRule] = {}
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(
                f"Строка {index}: нет знака «=». Формат: правильно = вариант, вариант"
            )
        target_part, variants_part = line.split("=", 1)
        target = target_part.strip()
        if not target:
            raise ValidationError(f"Строка {index}: не указана правильная форма слева от «=»")
        variants = dedupe_terms(variants_part.split(","))
        if not variants:
            raise ValidationError(f"Строка {index}: не указано ни одного варианта справа от «=»")
        key = target.casefold()
        existing = seen_targets.get(key)
        if existing is not None:
            # Merge repeated targets instead of shadowing one of them.
            existing.variants = dedupe_terms(existing.variants + variants)
            continue
        rule = ReplacementRule(to=target, variants=variants)
        seen_targets[key] = rule
        rules.append(rule)
    return rules


def serialize_replacements(rules: Iterable[ReplacementRule]) -> str:
    lines = [f"{rule.to} = {', '.join(rule.variants)}" for rule in rules if rule.variants]
    return "\n".join(lines)


def _boundary_pattern(variant: str) -> str:
    """Word boundaries that behave for Cyrillic, digits and multi-word phrases.

    A plain \\b fails when a variant starts or ends with a non-word character,
    so the guard is applied only on the sides where it makes sense.
    """
    escaped = re.escape(variant)
    prefix = r"(?<!\w)" if variant[:1].isalnum() or variant[:1] == "_" else ""
    suffix = r"(?!\w)" if variant[-1:].isalnum() or variant[-1:] == "_" else ""
    return prefix + escaped + suffix


def apply_replacements(text: str, rules: Iterable[ReplacementRule]) -> str:
    """Case-insensitive replacement; the target's own casing is written out.

    Longer variants are matched first so that "клод код" is not eaten by "клод".
    """
    if not text:
        return text
    pairs: list[tuple[str, str]] = []
    for rule in rules:
        for variant in rule.variants:
            if variant:
                pairs.append((variant, rule.to))
    if not pairs:
        return text
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    result = text
    for variant, target in pairs:
        pattern = re.compile(_boundary_pattern(variant), re.IGNORECASE | re.UNICODE)
        result = pattern.sub(lambda _m, value=target: value, result)
    return result


# --------------------------------------------------------------------------
# Bridge between the layers
# --------------------------------------------------------------------------

def build_api_vocabulary(
    vocabulary: Iterable[str], rules: Iterable[ReplacementRule]
) -> list[str]:
    """Terms sent to Gemini: the user list plus every replacement target."""
    combined = list(vocabulary) + [rule.to for rule in rules]
    return dedupe_terms(combined)
