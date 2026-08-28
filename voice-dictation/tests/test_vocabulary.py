import pytest

from src.config.model import ReplacementRule
from src.config.vocabulary import (
    ValidationError,
    build_api_vocabulary,
    parse_vocabulary_text,
    serialize_vocabulary,
    validate_replacements,
    validate_vocabulary,
    vocabulary_warnings,
)


def test_trims_and_drops_empty():
    assert parse_vocabulary_text("  n8n ,, Supabase ,  ") == ["n8n", "Supabase"]


def test_newlines_work_like_commas():
    assert parse_vocabulary_text("n8n\nSupabase, pgvector") == [
        "n8n", "Supabase", "pgvector"
    ]


def test_dedupe_keeps_first_casing():
    assert parse_vocabulary_text("Supabase, supabase, SUPABASE") == ["Supabase"]


def test_cyrillic_terms_survive():
    assert parse_vocabulary_text("Лунео, Говорилка") == ["Лунео", "Говорилка"]


def test_serialize_round_trip():
    terms = ["n8n", "Claude Code", "pgvector"]
    assert parse_vocabulary_text(serialize_vocabulary(terms)) == terms


def test_hard_limit_blocks_saving():
    terms = [f"term{i}" for i in range(1001)]
    with pytest.raises(ValidationError, match="более 1000"):
        validate_vocabulary(terms)


def test_exactly_thousand_is_allowed():
    terms = [f"term{i}" for i in range(1000)]
    assert validate_vocabulary(terms) == terms


def test_soft_limit_only_warns():
    terms = [f"term{i}" for i in range(101)]
    assert validate_vocabulary(terms) == terms
    assert vocabulary_warnings(terms)
    assert not vocabulary_warnings(terms[:100])


def test_api_vocabulary_merges_both_layers_without_duplicates():
    rules = [ReplacementRule("Claude Code", ["клоткот"]), ReplacementRule("n8n", ["н8н"])]
    merged = build_api_vocabulary(["Supabase", "claude code"], rules)
    assert merged == ["Supabase", "claude code", "n8n"]
    assert len([t for t in merged if t.casefold() == "claude code"]) == 1


def test_combined_list_is_what_the_limits_see():
    vocabulary = [f"term{i}" for i in range(90)]
    rules = [ReplacementRule(f"rule{i}", [f"вариант{i}"]) for i in range(600)]
    merged = build_api_vocabulary(vocabulary, rules)
    assert len(merged) == 690
    assert vocabulary_warnings(merged)
    many = [ReplacementRule(f"rule{i}", [f"вариант{i}"]) for i in range(1000)]
    with pytest.raises(ValidationError, match="более 1000"):
        validate_vocabulary(build_api_vocabulary(vocabulary, many))


def test_replacement_limit_is_separate_from_the_vocabulary_limit():
    rules = [ReplacementRule(f"rule{i}", [f"вариант{i}"]) for i in range(501)]
    with pytest.raises(ValidationError, match="больше 500"):
        validate_replacements(rules)
