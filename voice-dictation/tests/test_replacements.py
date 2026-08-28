import pytest

from src.config.model import ReplacementRule
from src.config.vocabulary import (
    ValidationError,
    apply_replacements,
    parse_replacements_text,
    serialize_replacements,
    validate_replacements,
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


def test_rules_do_not_cascade_into_each_other():
    rules = [
        ReplacementRule("Claude Code", ["клод"]),
        ReplacementRule("клод", ["клауд"]),
    ]
    assert apply_replacements("клауд", rules) == "клод"


def test_chained_rules_stop_after_one_pass():
    rules = [ReplacementRule("бб", ["аа"]), ReplacementRule("вв", ["бб"])]
    assert apply_replacements("аа", rules) == "бб"


def test_swap_pair_really_swaps():
    rules = [ReplacementRule("пёс", ["кот"]), ReplacementRule("кот", ["пёс"])]
    assert apply_replacements("кот и пёс", rules) == "пёс и кот"


def test_target_with_backreference_is_written_literally():
    rules = [ReplacementRule(r"\1", ["вариант"])]
    assert apply_replacements("это вариант", rules) == r"это \1"


def test_sentence_start_stays_capitalised():
    rules = [ReplacementRule("клод", ["клот"])]
    assert apply_replacements("Клот хороший", rules) == "Клод хороший"


def test_all_caps_match_capitalises_only_the_first_letter():
    rules = [ReplacementRule("клод", ["клот"])]
    assert apply_replacements("КЛОТ хороший", rules) == "Клод хороший"


def test_brand_casing_is_never_touched():
    rules = [ReplacementRule("Claude Code", ["клод код"])]
    assert apply_replacements("Клод код умеет", rules) == "Claude Code умеет"
    assert apply_replacements("это клод код", rules) == "это Claude Code"


def test_lowercase_target_stays_lowercase_mid_sentence():
    rules = [ReplacementRule("клод", ["клот"])]
    assert apply_replacements("это клот", rules) == "это клод"


def test_multiword_variant_matches_any_whitespace():
    rules = [ReplacementRule("X", ["привет мир"])]
    assert apply_replacements("привет  мир", rules) == "X"
    assert apply_replacements("привет\nмир", rules) == "X"


def test_replacement_hard_limit():
    rules = [ReplacementRule(f"t{i}", [f"v{i}"]) for i in range(501)]
    with pytest.raises(ValidationError, match="больше 500"):
        validate_replacements(rules)
    assert validate_replacements(rules[:500]) == rules[:500]


def test_expanding_case_fold_does_not_lose_the_dictation():
    # "İstanbul".casefold() is "i" + U+0307, but the alternation matches plain
    # "istanbul" under IGNORECASE: the lookup misses and used to raise.
    rules = [ReplacementRule("Стамбул", ["İstanbul"])]
    assert apply_replacements("istanbul kebab", rules) == "istanbul kebab"


def test_expanding_case_fold_still_replaces_the_exact_spelling():
    rules = [ReplacementRule("Стамбул", ["İstanbul"])]
    assert apply_replacements("İstanbul — город", rules) == "Стамбул — город"


def test_no_rule_can_make_a_replacement_raise():
    # Anything the model returns must come back out, replaced or not.
    rules = [
        ReplacementRule("Стамбул", ["İstanbul"]),
        ReplacementRule("улица", ["ΟΔΟΣ"]),
        ReplacementRule("улочка", ["Straße"]),
    ]
    for text in ("istanbul", "οδος", "ΟΔΌΣ", "strasse", "STRASSE", "ﬁle"):
        assert apply_replacements(text, rules)
