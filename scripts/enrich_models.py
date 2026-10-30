"""Conservatively merge verified benchmark rows into model records."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
import math
import numbers
import re

from validation import is_safe_external_url, parse_iso_datetime

BenchmarkRow = Mapping[str, object]
ModelRecord = Mapping[str, object]

_SEPARATORS = re.compile(r"[._\s]+")
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_SOURCE_NAME = "LMArena Arena Leaderboard"


def normalize_model_key(text: str) -> str:
    """Normalize provider-qualified model names without removing variants."""
    if not isinstance(text, str):
        return ""
    key = text.strip().lower()
    if "/" in key:
        key = key.split("/", 1)[1]
    key = _SEPARATORS.sub("-", key).strip("-")
    return _DATE_SUFFIX.sub("", key)


def _has_variant_suffix(model_id: str) -> bool:
    return ":" in model_id.split("/", 1)[-1]


def _match_details(
    models: Sequence[ModelRecord],
    rows: Sequence[BenchmarkRow],
) -> tuple[dict[str, BenchmarkRow], int, set[int], set[int]]:
    rows_by_key: dict[str, list[tuple[int, BenchmarkRow]]] = defaultdict(list)
    model_ids_by_key: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(rows):
        key = normalize_model_key(row.get("name", ""))
        if key:
            rows_by_key[key].append((index, row))

    for model in models:
        model_id = model.get("id")
        if isinstance(model_id, str) and model_id:
            key = normalize_model_key(model_id)
            if key:
                model_ids_by_key[key].add(model_id)

    matches: dict[str, BenchmarkRow] = {}
    ambiguous_row_indexes: set[int] = set()
    matched_row_indexes: set[int] = set()
    for key, candidates in rows_by_key.items():
        if len(candidates) > 1:
            ambiguous_row_indexes.update(index for index, _ in candidates)
            continue

        model_ids = model_ids_by_key.get(key, set())
        if not model_ids:
            continue
        if len(model_ids) > 1:
            unvarianted_ids = [
                model_id for model_id in model_ids
                if not _has_variant_suffix(model_id)
            ]
            if len(unvarianted_ids) != 1:
                ambiguous_row_indexes.add(candidates[0][0])
                continue
            selected_id = unvarianted_ids[0]
        else:
            selected_id = next(iter(model_ids))

        row_index, row = candidates[0]
        matches[selected_id] = row
        matched_row_indexes.add(row_index)

    return matches, len(ambiguous_row_indexes), ambiguous_row_indexes, matched_row_indexes


def match_benchmarks(
    models: Sequence[ModelRecord],
    rows: Sequence[BenchmarkRow],
) -> dict[str, BenchmarkRow]:
    """Match only unique, exactly normalized model keys."""
    matches, _, _, _ = _match_details(models, rows)
    return matches


def apply_benchmarks(
    models: Sequence[ModelRecord],
    rows: Sequence[BenchmarkRow],
    *,
    previous_models: Sequence[ModelRecord] | None = None,
    arena_succeeded: bool,
) -> list[dict[str, object]]:
    """Return copied model records with fresh values or last-known-good values."""
    result = [dict(model) for model in models]
    if arena_succeeded:
        matches = match_benchmarks(models, rows)
        for model in result:
            model_id = model.get("id")
            row = matches.get(model_id) if isinstance(model_id, str) else None
            model["arenaElo"] = row.get("arena_elo") if row is not None else None
            model["codingElo"] = row.get("coding_elo") if row is not None else None
        return result

    previous_by_id = {
        model_id: model
        for model in previous_models or ()
        if isinstance((model_id := model.get("id")), str)
    }
    for model in result:
        model_id = model.get("id")
        previous = previous_by_id.get(model_id) if isinstance(model_id, str) else None
        if previous is None:
            continue
        for output_key in ("arenaElo", "codingElo"):
            previous_value = previous.get(output_key)
            if previous_value is not None:
                model[output_key] = previous_value
    return result


def build_provenance(
    rows: Sequence[BenchmarkRow],
    fetched_iso: str,
) -> dict[str, str | None] | None:
    """Build validated benchmark provenance when rows agree on one snapshot."""
    if not rows or parse_iso_datetime(fetched_iso) is None:
        return None

    first_row = rows[0]
    source_url = first_row.get("source_url")
    snapshot_iso = first_row.get("snapshot_iso")
    if (not isinstance(source_url, str) or not is_safe_external_url(source_url)
            or (snapshot_iso is not None and (
                not isinstance(snapshot_iso, str)
                or parse_iso_datetime(snapshot_iso) is None
            ))):
        return None
    if any(
        row.get("source_url") != source_url
        or row.get("snapshot_iso") != snapshot_iso
        for row in rows
    ):
        return None

    return {
        "source": _SOURCE_NAME,
        "sourceUrl": source_url,
        "snapshotIso": snapshot_iso,
        "fetchedIso": fetched_iso,
    }


def build_match_report(
    models: Sequence[ModelRecord],
    rows: Sequence[BenchmarkRow],
) -> dict[str, object]:
    """Summarize exact matches and highest-Elo unmatched benchmark rows."""
    matches, ambiguous_count, _, matched_row_indexes = _match_details(models, rows)
    unmatched_rows = [
        row for index, row in enumerate(rows)
        if index not in matched_row_indexes
    ]

    def arena_elo(row: BenchmarkRow) -> float:
        value = row.get("arena_elo")
        if (isinstance(value, numbers.Real) and not isinstance(value, bool)
                and math.isfinite(value)):
            return float(value)
        return float("-inf")

    top_unmatched = sorted(unmatched_rows, key=arena_elo, reverse=True)[:20]
    unmatched_model_count = sum(
        not (isinstance(model.get("id"), str) and model.get("id") in matches)
        for model in models
    )
    return {
        "matched_count": len(matches),
        "ambiguous_count": ambiguous_count,
        "unmatched_openrouter_count": unmatched_model_count,
        "top_unmatched_arena_rows": [dict(row) for row in top_unmatched],
    }
