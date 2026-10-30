"""Synthetic-only tests; no live OpenRouter or benchmark records are used."""

from copy import deepcopy

import pytest

from enrich_models import (
    apply_benchmarks,
    build_match_report,
    build_provenance,
    match_benchmarks,
    normalize_model_key,
)
from validation import validate_metadata


def model(model_id, **values):
    return {"id": model_id, "arenaElo": None, "codingElo": None, **values}


def benchmark(name, arena_elo=None, coding_elo=None):
    return {
        "name": name,
        "arena_elo": arena_elo,
        "coding_elo": coding_elo,
        "source_url": "https://example.invalid/synthetic-arena.csv",
        "snapshot_iso": "2026-01-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("anthropic/claude-3.5-sonnet", "claude-3-5-sonnet"),
        ("openai/gpt-4o", "gpt-4o"),
        ("google/gemini-2.5-pro", "gemini-2-5-pro"),
        ("deepseek/deepseek-r1", "deepseek-r1"),
        ("meta-llama/llama-3.3-70b-instruct", "llama-3-3-70b-instruct"),
        ("qwen/qwen-2.5-coder-32b-instruct", "qwen-2-5-coder-32b-instruct"),
        (" Provider/Model_Name 2024 ", "model-name-2024"),
        ("provider/model_20240513", "model"),
        ("provider/model-2024-05-13", "model"),
        ("provider/model-mini-pro-flash-70b-thinking", "model-mini-pro-flash-70b-thinking"),
    ],
)
def test_normalize_model_key(text, expected):
    assert normalize_model_key(text) == expected


def test_exact_matching_does_not_collide_or_match_non_equivalent_claude_name():
    models = [
        model("openai/gpt-4o"),
        model("openai/gpt-4o-mini"),
        model("anthropic/claude-3.5-sonnet"),
    ]
    rows = [
        benchmark("gpt-4o", 1501),
        benchmark("gpt-4o-mini", 1502),
        benchmark("claude-3-5-sonnet", 1503),
        benchmark("claude-3-5-sonnet-pro", 1504),
    ]

    matches = match_benchmarks(models, rows)

    assert matches == {
        "openai/gpt-4o": rows[0],
        "openai/gpt-4o-mini": rows[1],
        "anthropic/claude-3.5-sonnet": rows[2],
    }


def test_variants_do_not_inherit_base_model_scores():
    models = [
        model("provider/example-model"),
        model("provider/example-model:free"),
        model("provider/example-model:thinking"),
    ]
    rows = [benchmark("example-model", 1510, 1520)]

    matches = match_benchmarks(models, rows)
    result = apply_benchmarks(models, rows, arena_succeeded=True)

    assert set(matches) == {"provider/example-model"}
    assert result[0]["arenaElo"] == 1510
    assert result[1]["arenaElo"] is None
    assert result[2]["arenaElo"] is None


def test_duplicate_benchmark_keys_are_ambiguous():
    models = [model("provider/example-model")]
    rows = [benchmark("example.model", 1501), benchmark("example_model", 1502)]

    assert match_benchmarks(models, rows) == {}
    report = build_match_report(models, rows)
    assert report["ambiguous_count"] == 2
    assert report["matched_count"] == 0


def test_multiple_openrouter_ids_with_one_normalized_key_are_skipped():
    models = [model("provider-a/shared"), model("provider-b/shared")]
    rows = [benchmark("shared", 1510)]

    assert match_benchmarks(models, rows) == {}
    assert build_match_report(models, rows)["ambiguous_count"] == 1


def test_success_is_authoritative_and_unknown_values_stay_null():
    models = [
        model("provider/matched", arenaElo=1800, codingElo=1700),
        model("provider/unmatched", arenaElo=1600, codingElo=1500),
    ]
    rows = [benchmark("matched", 1530, None)]

    result = apply_benchmarks(models, rows, arena_succeeded=True)

    assert result[0]["arenaElo"] == 1530
    assert result[0]["codingElo"] is None
    assert result[1]["arenaElo"] is None
    assert result[1]["codingElo"] is None


def test_failure_retains_previous_non_null_values_by_id():
    models = [
        model("provider/retained"),
        model("provider/current-value", arenaElo=1590, codingElo=1490),
        model("provider/new-model"),
    ]
    previous_models = [
        model("provider/retained", arenaElo=1701, codingElo=1601),
        model("provider/current-value"),
    ]

    result = apply_benchmarks(
        models,
        [],
        previous_models=previous_models,
        arena_succeeded=False,
    )

    assert result[0]["arenaElo"] == 1701
    assert result[0]["codingElo"] == 1601
    assert result[1]["arenaElo"] == 1590
    assert result[1]["codingElo"] == 1490
    assert result[2]["arenaElo"] is None
    assert result[2]["codingElo"] is None


def test_applying_benchmarks_does_not_mutate_inputs():
    models = [model("provider/model", categoryTags=["test"])]
    previous_models = [model("provider/model", arenaElo=1700)]
    rows = [benchmark("model", 1500)]
    before = deepcopy((models, previous_models, rows))

    apply_benchmarks(
        models,
        rows,
        previous_models=previous_models,
        arena_succeeded=True,
    )

    assert (models, previous_models, rows) == before


def test_build_provenance_validates_when_embedded_in_metadata():
    rows = [benchmark("synthetic-model", 1500)]
    provenance = build_provenance(rows, "2026-01-02T00:00:00Z")
    metadata = {
        "schemaVersion": 1,
        "dataOrigin": "ingested",
        "lastUpdatedIso": "2026-01-02T00:00:00Z",
        "lastAttemptIso": None,
        "counts": {"news": 0, "models": 0, "builds": 0, "hackathons": 0},
        "sources": {},
        "benchmarks": provenance,
    }

    assert provenance is not None
    assert validate_metadata(metadata) == (True, None)
    assert build_provenance([], "2026-01-02T00:00:00Z") is None
    assert build_provenance(rows, "not-a-date") is None


def test_match_report_lists_top_twenty_unmatched_rows_by_arena_elo():
    rows = [benchmark(f"unlisted-{score}", score) for score in range(1500, 1525)]

    report = build_match_report([], rows)

    assert report["matched_count"] == 0
    assert report["unmatched_openrouter_count"] == 0
    top_rows = report["top_unmatched_arena_rows"]
    assert len(top_rows) == 20
    assert [row["arena_elo"] for row in top_rows] == list(range(1524, 1504, -1))
