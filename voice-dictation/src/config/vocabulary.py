"""Two-layer vocabulary: API bias list + local deterministic replacements.

Pure functions only - no Qt, no WinAPI, no network. Fully unit tested.
"""

from __future__ import annotations

import re
from typing import Iterable

from .model import ReplacementRule

VOCABULARY_HARD_LIMIT = 1000
VOCABULARY_SOFT_LIMIT = 100
REPLACEMENTS_HARD_LIMIT = 500


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


def validate_replacements(rules: list[ReplacementRule]) -> list[ReplacementRule]:
    """Hard limit on the replacement table; every target also goes to the API."""
    if len(rules) > REPLACEMENTS_HARD_LIMIT:
        raise ValidationError("Правил замен больше 500 — оставьте не больше 500 строк")
    return rules


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
    so the guard is applied only on the sides where it makes sense. Whitespace
    inside a variant matches any run of whitespace: the model may return a
    double space or a line break between the words of a phrase.
    """
    escaped = r"\s+".join(re.escape(part) for part in variant.split())
    prefix = r"(?<!\w)" if variant[:1].isalnum() or variant[:1] == "_" else ""
    suffix = r"(?!\w)" if variant[-1:].isalnum() or variant[-1:] == "_" else ""
    return prefix + escaped + suffix


def _variant_key(text: str) -> str:
    """Lookup key shared by a variant and the text that matched it."""
    return " ".join(text.split()).casefold()


def _apply_casing(matched: str, target: str) -> str:
    """Keep a lowercase target from lowercasing the start of a sentence.

    Only the first character is ever touched, and only when the target is
    itself all-lowercase - casing like "Claude Code" is deliberate and stays
    verbatim. ALL-CAPS input is treated exactly like a capitalised word: the
    model shouting is not a reason to shout the target back, and an all-caps
    brand would be wrong anyway.
    """
    if not matched[:1].isupper() or not target.islower():
        return target
    return target[:1].upper() + target[1:]


def apply_replacements(text: str, rules: Iterable[ReplacementRule]) -> str:
    """Case-insensitive replacement; the target's own casing is written out.

    One pass over the text with a single alternation: every rule sees the
    ORIGINAL text, so rules can never cascade into each other (a swap pair
    "пёс = кот" / "кот = пёс" really swaps). Longer variants come first in the
    alternation, which gives leftmost-longest matching.
    """
    if not text:
        return text
    pairs: list[tuple[str, str]] = []
    for rule in rules:
        for variant in rule.variants:
            if variant.strip():
                pairs.append((variant, rule.to))
    if not pairs:
        return text
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    targets: dict[str, str] = {}
    alternatives: list[str] = []
    for variant, target in pairs:
        key = _variant_key(variant)
        if key in targets:
            continue  # the longer, earlier rule already claimed this spelling
        targets[key] = target
        alternatives.append(_boundary_pattern(variant))

    pattern = re.compile("|".join(alternatives), re.IGNORECASE | re.UNICODE)

    def substitute(match: "re.Match[str]") -> str:
        matched = match.group(0)
        # Written literally: a target containing \1 must not become a backreference.
        return _apply_casing(matched, targets[_variant_key(matched)])

    return pattern.sub(substitute, text)


# --------------------------------------------------------------------------
# Bridge between the layers
# --------------------------------------------------------------------------

def build_api_vocabulary(
    vocabulary: Iterable[str], rules: Iterable[ReplacementRule]
) -> list[str]:
    """Terms sent to Gemini: the user list plus every replacement target."""
    combined = list(vocabulary) + [rule.to for rule in rules]
    return dedupe_terms(combined)
