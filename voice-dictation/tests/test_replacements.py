import pytest

from src.config.model import ReplacementRule
from src.config.vocabulary import (
    ValidationError,
    apply_replacements,
    parse_replacements_text,
    serialize_replacements,
)

SAMPLE = """
# комментарий
Claude Code = клод код, клоткот, клод коуд
n8n = эн восемь эн, н8н

Supabase = супабейс, супа база
"""


def test_parses_rules_and_skips_comments_and_blanks():
    rules = parse_replacements_text(SAMPLE)
    assert [rule.to for rule in rules] == ["Claude Code", "n8n", "Supabase"]
    assert rules[0].variants == ["клод код", "клоткот", "клод коуд"]


def test_splits_on_the_first_equals_only():
    rules = parse_replacements_text("a = b = c, d")
    assert rules[0].to == "a"
    assert rules[0].variants == ["b = c", "d"]


def test_line_without_equals_names_its_number():
    with pytest.raises(ValidationError, match="Строка 2"):
        parse_replacements_text("ok = вариант\nбез знака равенства")


def test_empty_sides_are_errors():
    with pytest.raises(ValidationError, match="Строка 1"):
        parse_replacements_text(" = вариант")
    with pytest.raises(ValidationError, match="Строка 1"):
        parse_replacements_text("Claude Code = ")


def test_repeated_target_merges_variants():
    rules = parse_replacements_text("n8n = н8н\nn8n = ноден")
    assert len(rules) == 1
    assert rules[0].variants == ["н8н", "ноден"]


def test_serialize_round_trip():
    rules = parse_replacements_text(SAMPLE)
    assert parse_replacements_text(serialize_replacements(rules)) == rules


def test_replacement_is_case_insensitive_and_keeps_target_casing():
    rules = [ReplacementRule("Claude Code", ["клод код"])]
    assert apply_replacements("Открой КЛОД КОД сейчас", rules) == "Открой Claude Code сейчас"


def test_longer_variant_wins_over_shorter():
    rules = [ReplacementRule("Claude Code", ["клод код"]), ReplacementRule("Клод", ["клод"])]
    assert apply_replacements("запусти клод код", rules) == "запусти Claude Code"


def test_word_boundaries_hold_for_cyrillic():
    rules = [ReplacementRule("n8n", ["ноден"])]
    assert apply_replacements("ноденом пользуюсь", rules) == "ноденом пользуюсь"
    assert apply_replacements("ноден работает", rules) == "n8n работает"


def test_terms_with_digits():
    rules = [ReplacementRule("n8n", ["н8н"])]
    assert apply_replacements("поставь н8н", rules) == "поставь n8n"
    assert apply_replacements("н8нщик", rules) == "н8нщик"


def test_multiword_variants_and_punctuation():
    rules = [ReplacementRule("Supabase", ["супа база"])]
    assert apply_replacements("это супа база, точно.", rules) == "это Supabase, точно."


def test_empty_text_and_no_rules():
    assert apply_replacements("", [ReplacementRule("a", ["b"])]) == ""
    assert apply_replacements("текст", []) == "текст"


def test_target_containing_a_variant_does_not_loop():
    rules = [ReplacementRule("Claude Code", ["клод"])]
    assert apply_replacements("клод", rules) == "Claude Code"
