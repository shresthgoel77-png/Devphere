"""Run Phase 2 sources and publish validated JSON; archiving is deferred."""

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
import math
from pathlib import Path
import sys
import time
from typing import Callable

import httpx

import storage
from enrich_models import apply_benchmarks, build_provenance
from fetching import FetchConfig, new_client
from models import DATASET_NAMES, SOURCE_STATUSES, SourceResult, sanitize_error
from sources import devto, hacker_news, huggingface, openrouter, reddit, youtube
from validation import (
    is_safe_external_url,
    parse_iso_datetime,
    validate_dataset_items,
    validate_envelope,
    validate_metadata,
)

LOGGER = logging.getLogger("devnexus.ingest")


class UsageError(ValueError):
    """Invalid CLI or run configuration, distinct from operational failures."""


# Retention/pruning limits are intentionally named and can be tuned per phase.
NEWS_RETENTION_DAYS = 90
NEWS_MAX_ITEMS = 500
MODELS_MAX_ITEMS = 1000
MAX_DATASET_BYTES = 2_000_000
STALE_AFTER_HOURS = 18
MAX_REJECTION_EXAMPLES = 3
MAX_REJECTION_REASONS = 8

NEWS_SOURCE_IDS = ("hackernews", "devto", "huggingface", "youtube", "reddit")
MODEL_SOURCE_IDS = ("openrouter",)
DATASET_SOURCE_IDS = {
    "news": NEWS_SOURCE_IDS,
    "models": MODEL_SOURCE_IDS,
}

SOURCE_REGISTRY: dict[str, object] = {
    "hackernews": hacker_news, "devto": devto, "huggingface": huggingface,
    "youtube": youtube, "reddit": reddit, "openrouter": openrouter,
}
if (Path(__file__).resolve().parent / "sources" / "arena.py").is_file():
    SOURCE_REGISTRY["arena"] = importlib.import_module("sources.arena")
    MODEL_SOURCE_IDS = (*MODEL_SOURCE_IDS, "arena")
    DATASET_SOURCE_IDS["models"] = MODEL_SOURCE_IDS

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "public" / "data"
_FAILURE_STATUSES = {"failed", "rate_limited", "stale", "partial"}
_SAFE_ERROR_SUMMARIES = {
    sanitize_error(error)
    for error in (
        TimeoutError(),
        OSError(),
        ValueError(),
        TypeError(),
        RuntimeError(),
        None,
    )
}
_SOURCE_TO_DATASET = {
    source_id: dataset
    for dataset, source_ids in DATASET_SOURCE_IDS.items()
    for source_id in source_ids
}


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UsageError("now must be timezone-aware.")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _fetch_function(source: object) -> Callable[..., object]:
    fetcher = getattr(source, "fetch", source)
    if not callable(fetcher):
        raise UsageError("Every source registry entry must be callable or define fetch().")
    return fetcher


def _resolve_sources(requested: Sequence[str] | None, registry: Mapping[str, object]) -> list[str]:
    selected = list(registry) if requested is None else list(requested)
    if not selected or any(not isinstance(value, str) or not value.strip() for value in selected):
        raise UsageError("At least one valid source ID must be selected.")
    if len(set(selected)) != len(selected):
        raise UsageError("Source IDs must not be repeated.")
    if set(selected) - set(registry):
        raise UsageError("Unknown or unavailable source ID.")
    if any(source_id not in _SOURCE_TO_DATASET and source_id != "arena" for source_id in selected):
        raise UsageError("Source is not registered for a Phase 2 dataset.")
    for source_id in selected:
        _fetch_function(registry[source_id])
    return selected


def _source_dataset(source_id: str) -> str:
    if source_id == "arena":
        return "models"
    return _SOURCE_TO_DATASET[source_id]


def _failure_result(source_id: str, error: BaseException, started: datetime,
                    latency_ms: float) -> SourceResult:
    result = SourceResult.failure(source_id, error, started_iso=_iso_utc(started),
                                  finished_iso=_iso_utc(datetime.now(timezone.utc)),
                                  latency_ms=max(0.0, latency_ms))
    result.error_summary = sanitize_error(error)
    return result


async def _fetch_one(
    source_id: str,
    source: object,
    client: httpx.AsyncClient,
    config: FetchConfig,
    now: datetime,
) -> SourceResult:
    started_clock = time.monotonic()
    try:
        value = await asyncio.wait_for(_fetch_function(source)(client, config, now),
                                       timeout=config.total_budget_s)
        elapsed_ms = max(0.0, (time.monotonic() - started_clock) * 1000)
        if not isinstance(value, SourceResult):
            raise TypeError("Source returned an invalid result.")
        if value.source_id != source_id:
            raise ValueError("Source result ID did not match its registry key.")
        if value.status not in SOURCE_STATUSES or not isinstance(value.records, list):
            raise ValueError("Source returned an invalid result shape.")
        if value.latency_ms is None or not _finite_nonnegative(value.latency_ms):
            value.latency_ms = elapsed_ms
        if value.error_summary and value.error_summary not in _SAFE_ERROR_SUMMARIES:
            value.error_summary = sanitize_error(value.error_summary)
        return value
    except asyncio.CancelledError:
        raise
    except Exception as error:
        elapsed_ms = max(0.0, (time.monotonic() - started_clock) * 1000)
        return _failure_result(source_id, error, now, elapsed_ms)


def _finite_nonnegative(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _valid_benchmark_value(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                             and math.isfinite(value) and 500 <= value <= 3000)


def _validate_benchmark_rows(records: list[object]) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            rejected.append({"index": index, "reasons": ["record must be an object."]})
            continue
        name = row.get("name")
        source_url = row.get("source_url")
        snapshot_iso = row.get("snapshot_iso")
        valid_snapshot = snapshot_iso is None or parse_iso_datetime(snapshot_iso) is not None
        reasons = []
        if not isinstance(name, str) or not name.strip():
            reasons.append("name must be a non-empty string.")
        if not _valid_benchmark_value(row.get("arena_elo")):
            reasons.append("arena_elo must be null or a finite value from 500 through 3000.")
        if not _valid_benchmark_value(row.get("coding_elo")):
            reasons.append("coding_elo must be null or a finite value from 500 through 3000.")
        if not is_safe_external_url(source_url):
            reasons.append("source_url must be a safe HTTPS URL.")
        if not valid_snapshot:
            reasons.append("snapshot_iso must be null or a valid ISO date-time.")
        if reasons:
            rejected.append({"index": index, "reasons": reasons})
        else:
            accepted.append(dict(row))
    return accepted, rejected


def _source_summary(
    result: SourceResult,
    accepted_count: int,
    rejected_examples: list[dict],
    rejected_count: int,
    reason_counts: Counter[str],
) -> dict:
    rejection_total = rejected_count
    status = result.status
    if status in ("success", "no_data", "partial"):
        status = ("partial" if accepted_count and rejection_total
                  else "failed" if rejection_total and status != "partial"
                  else "no_data" if not accepted_count and status != "partial"
                  else status)
    error_summary = result.error_summary
    if error_summary and error_summary not in _SAFE_ERROR_SUMMARIES:
        error_summary = sanitize_error(error_summary)
    if error_summary is None and rejection_total:
        error_summary = f"Validation rejected {rejection_total} source record(s)."
    return {"status": status, "records_fetched": len(result.records),
            "records_accepted": accepted_count, "records_rejected": rejection_total,
            "rejection_reason_counts": dict(reason_counts.most_common(MAX_REJECTION_REASONS)),
            "rejection_examples": rejected_examples[:MAX_REJECTION_EXAMPLES],
            "latency_ms": result.latency_ms, "error_summary": error_summary}


def _compact_rejections(report: object, total_records: int, accepted_records: int) -> tuple[int, Counter[str], list[dict]]:
    rejected = getattr(report, "rejected", [])
    reasons: Counter[str] = Counter()
    examples = []
    for entry in rejected:
        entry_reasons = list(getattr(entry, "reasons", []))
        reasons.update(entry_reasons)
        if len(examples) < MAX_REJECTION_EXAMPLES and entry_reasons:
            examples.append({"index": getattr(entry, "index", None),
                             "reasons": entry_reasons[:MAX_REJECTION_REASONS]})
    return max(0, total_records - accepted_records), reasons, examples


def _load_previous(data_dir: Path) -> tuple[dict[str, dict | None], dict | None]:
    datasets = {name: storage.load_published(data_dir / f"{name}.json")[0] for name in DATASET_NAMES}
    metadata, _ = storage.load_published(data_dir / "metadata.json")
    return datasets, metadata


def _previous_non_seed(dataset: str, envelope: dict | None) -> list[dict]:
    if envelope is None:
        return []
    if envelope["dataOrigin"] == "seed":
        return []
    return [item for item in envelope["items"]
            if not isinstance(item.get("id"), str) or not item["id"].startswith("seed-")]


def _order_news(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (-parse_iso_datetime(item["publishedAtIso"]).timestamp(), item["id"]))


def _prune_news(items: list[dict], now: datetime) -> tuple[list[dict], dict[str, int]]:
    cutoff = now - timedelta(days=NEWS_RETENTION_DAYS)
    recent = [item for item in items if parse_iso_datetime(item["publishedAtIso"]).astimezone(timezone.utc) >= cutoff]
    age_pruned = len(items) - len(recent)
    ordered = _order_news(recent)
    cap_pruned = max(0, len(ordered) - NEWS_MAX_ITEMS)
    return ordered[:NEWS_MAX_ITEMS], {"older_than_retention": age_pruned, "over_limit": cap_pruned}


def _prune_models(items: list[dict]) -> tuple[list[dict], dict[str, int]]:
    ordered = sorted(items, key=lambda item: item["id"])
    cap_pruned = max(0, len(ordered) - MODELS_MAX_ITEMS)
    return ordered[:MODELS_MAX_ITEMS], {"older_than_retention": 0, "over_limit": cap_pruned}


def _actual_counts(data_dir: Path) -> tuple[dict[str, int], dict[str, dict | None]]:
    datasets = {name: storage.load_published(data_dir / f"{name}.json")[0] for name in DATASET_NAMES}
    counts = {name: len(data["items"]) if data is not None else 0 for name, data in datasets.items()}
    return counts, datasets


def _new_metadata(
    previous: dict | None,
    source_summaries: dict[str, dict],
    counts: dict[str, int],
    on_disk: dict[str, dict | None],
    *,
    now: datetime,
    data_changed: bool,
    provenance: dict | None,
) -> dict:
    now_iso = _iso_utc(now)
    sources = dict(previous.get("sources", {})) if previous is not None else {}
    for source_id, summary in source_summaries.items():
        old = sources.get(source_id, {})
        last_success = old.get("lastSuccessIso")
        if summary["records_accepted"] > 0:
            last_success = now_iso
        status = summary["status"]
        previous_success = parse_iso_datetime(last_success)
        if (status in ("failed", "rate_limited") and previous_success is not None
                and now - previous_success.astimezone(timezone.utc) > timedelta(hours=STALE_AFTER_HOURS)):
            status = "stale"
        sources[source_id] = {
            "status": status,
            "lastSuccessIso": last_success,
            "lastAttemptIso": now_iso,
            "recordsFetched": summary["records_fetched"],
            "recordsAccepted": summary["records_accepted"],
            "errorSummary": summary["error_summary"] or (
                f"Validation rejected {summary['records_rejected']} source record(s)."
                if summary["records_rejected"] else None
            ),
        }

    return {
        "schemaVersion": 1,
        "dataOrigin": ("ingested" if all(on_disk[name] is not None
                                        and on_disk[name]["dataOrigin"] == "ingested"
                                        for name in DATASET_NAMES) else "seed"),
        "lastUpdatedIso": previous["lastUpdatedIso"] if previous is not None and not data_changed else now_iso,
        "lastAttemptIso": now_iso,
        "counts": counts,
        "sources": sources,
        **({"benchmarks": provenance or previous.get("benchmarks")}
           if provenance is not None or previous and previous.get("benchmarks") is not None else {}),
    }


def _candidate_outcome(
    dataset: str,
    items: list[dict],
    data_origin: str,
    generated_at: str,
    path: Path,
    previous: dict | None,
    *,
    max_items: int,
    dry_run: bool,
) -> tuple[str, str, bool]:
    candidate = {"schemaVersion": 1, "dataOrigin": data_origin,
                 "generatedAtIso": generated_at, "items": items}
    valid, reason = validate_envelope(dataset, candidate)
    if not valid:
        return "refused", reason or "Candidate envelope is invalid.", False
    report = validate_dataset_items(dataset, items)
    if len(report.accepted) != len(items):
        return "refused", "Candidate contains rejected records.", False
    if len(items) > max_items:
        return "refused", "Candidate exceeds max_items.", False
    content = storage.serialize_envelope(report.accepted, data_origin, generated_at)
    if len(content.encode("utf-8")) > MAX_DATASET_BYTES:
        return "refused", "Candidate exceeds max_bytes.", False
    if dry_run and not items and previous is not None and previous["items"]:
        return "refused", "Empty candidate cannot replace valid non-empty data.", False
    current = path.read_bytes() if path.exists() else None
    if current == content.encode("utf-8"):
        return "unchanged", "Content is unchanged.", False
    return (("skipped", "Dry run: candidate validated but was not written.", False)
            if dry_run else ("publish", "Candidate passed pre-publication checks.", True))


async def run_ingestion(
    data_dir: str | Path = _DEFAULT_DATA_DIR,
    *,
    sources: Sequence[str] | None = None,
    dry_run: bool = False,
    strict: bool = False,
    summary_json: str | Path | None = None,
    source_registry: Mapping[str, object] | None = None,
    config: FetchConfig | None = None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    """Run selected sources and return ``(exit_code, summary)``."""
    registry = SOURCE_REGISTRY if source_registry is None else source_registry
    selected = _resolve_sources(sources, registry)
    run_at = _as_utc(now or datetime.now(timezone.utc))
    run_iso = _iso_utc(run_at)
    fetch_config = config or FetchConfig()
    target_dir = Path(data_dir).expanduser().resolve()
    summary_path = Path(summary_json).expanduser().resolve() if summary_json is not None else None
    if target_dir.exists() and not target_dir.is_dir():
        raise UsageError("--data-dir must be a directory.")
    if summary_path is not None and summary_path in {
        target_dir / f"{name}.json" for name in (*DATASET_NAMES, "metadata")
    }:
        raise UsageError("--summary-json cannot overwrite a published data or metadata file.")
    if dry_run and summary_path is not None:
        try:
            summary_path.relative_to(target_dir)
        except ValueError:
            pass
        else:
            raise UsageError("--summary-json must be outside --data-dir during --dry-run.")
    if not dry_run:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise UsageError("Unable to create --data-dir.") from error

    previous_datasets, previous_metadata = _load_previous(target_dir)
    async with new_client(fetch_config) as client:
        results = await asyncio.gather(*(
            _fetch_one(source_id, registry[source_id], client, fetch_config, run_at)
            for source_id in selected
        ))
    source_results = dict(zip(selected, results))

    accepted_by_source: dict[str, list[dict]] = {}
    source_summaries: dict[str, dict] = {}
    benchmark_rows: list[dict] = []

    for source_id in selected:
        result = source_results[source_id]
        dataset = _source_dataset(source_id)
        if source_id == "arena":
            accepted, invalid_rows = _validate_benchmark_rows(result.records)
            benchmark_rows.extend(accepted)
            rejected_examples = invalid_rows[:MAX_REJECTION_EXAMPLES]
            reason_counts = Counter(
                reason for row in invalid_rows for reason in row["reasons"]
            )
            source_summaries[source_id] = _source_summary(
                result,
                len(accepted),
                rejected_examples,
                len(invalid_rows),
                reason_counts,
            )
            continue

        report = validate_dataset_items(dataset, result.records)
        rejected_count, reason_counts, rejected_examples = _compact_rejections(
            report,
            len(result.records),
            len(report.accepted),
        )
        accepted_records = report.accepted
        accepted_by_source[source_id] = accepted_records
        source_summaries[source_id] = _source_summary(
            result,
            len(accepted_records),
            rejected_examples,
            rejected_count,
            reason_counts,
        )

    previous_source_statuses = (
        previous_metadata.get("sources", {})
        if previous_metadata is not None
        else {}
    )
    for source_id, entry in source_summaries.items():
        if entry["status"] not in ("failed", "rate_limited"):
            continue
        previous_entry = previous_source_statuses.get(source_id, {})
        last_success = parse_iso_datetime(previous_entry.get("lastSuccessIso"))
        if (last_success is not None
                and run_at - last_success.astimezone(timezone.utc)
                > timedelta(hours=STALE_AFTER_HOURS)):
            entry["status"] = "stale"

    source_errors = any(
        entry["status"] in _FAILURE_STATUSES
        for entry in source_summaries.values()
    )
    per_source = {}
    for source_id in selected:
        entry = source_summaries[source_id]
        per_source[source_id] = entry
        LOGGER.info(
            "Source %s: status=%s fetched=%d accepted=%d rejected=%d latency_ms=%.1f",
            source_id,
            entry["status"],
            entry["records_fetched"],
            entry["records_accepted"],
            entry["records_rejected"],
            entry["latency_ms"],
        )

    selected_by_dataset = {name: [sid for sid in selected if _source_dataset(sid) == name]
                           for name in DATASET_NAMES}
    dataset_outcomes: dict[str, dict] = {}
    pruned_counts = {
        "news": {"older_than_retention": 0, "over_limit": 0},
        "models": {"older_than_retention": 0, "over_limit": 0},
    }
    data_changed = False
    metadata_provenance = None

    for dataset in ("news", "models"):
        dataset_sources = selected_by_dataset[dataset]
        new_records = [
            record
            for source_id in dataset_sources
            for record in accepted_by_source.get(source_id, [])
        ]
        if not dataset_sources:
            dataset_outcomes[dataset] = {"status": "skipped", "reason": "No sources selected."}
            LOGGER.info("Dataset %s: skipped (no sources selected)", dataset)
            continue
        if dataset == "models" and not accepted_by_source.get("openrouter"):
            dataset_outcomes[dataset] = {
                "status": "skipped",
                "reason": "OpenRouter produced no accepted model records.",
            }
            LOGGER.info("Dataset models: skipped (no accepted OpenRouter models)")
            continue
        if not new_records:
            dataset_outcomes[dataset] = {
                "status": "skipped",
                "reason": "No source produced accepted records.",
            }
            LOGGER.info("Dataset %s: skipped (no accepted records)", dataset)
            continue

        previous_envelope = previous_datasets[dataset]
        previous_items = _previous_non_seed(dataset, previous_envelope)
        candidate_items = storage.merge_with_previous(previous_items, new_records)
        if dataset == "models":
            arena_was_selected = "arena" in selected
            arena_status = source_summaries.get("arena", {}).get("status")
            arena_succeeded = arena_was_selected and arena_status in ("success", "no_data")
            candidate_items = apply_benchmarks(
                candidate_items,
                benchmark_rows,
                previous_models=previous_items,
                arena_succeeded=arena_succeeded,
            )
            if arena_succeeded and benchmark_rows:
                metadata_provenance = build_provenance(benchmark_rows, run_iso)

        if dataset == "news":
            candidate_items, pruned_counts[dataset] = _prune_news(candidate_items, run_at)
            max_items = NEWS_MAX_ITEMS
        else:
            candidate_items, pruned_counts[dataset] = _prune_models(candidate_items)
            max_items = MODELS_MAX_ITEMS

        data_origin = "ingested"
        generated_at = run_iso
        if (previous_envelope is not None
                and previous_envelope["dataOrigin"] == data_origin
                and previous_envelope["items"] == candidate_items):
            generated_at = previous_envelope["generatedAtIso"]

        path = target_dir / f"{dataset}.json"
        outcome, reason, should_publish = _candidate_outcome(
            dataset,
            candidate_items,
            data_origin,
            generated_at,
            path,
            previous_envelope,
            max_items=max_items,
            dry_run=dry_run,
        )
        if outcome == "publish" and should_publish:
            published = storage.publish_dataset(
                dataset,
                candidate_items,
                data_origin,
                generated_at,
                path,
                max_items=max_items,
                max_bytes=MAX_DATASET_BYTES,
                allow_empty=False,
            )
            if published.published:
                outcome = "published"
                data_changed = True
            elif published.unchanged:
                outcome = "unchanged"
                reason = published.reason
            else:
                outcome = "refused"
                reason = published.reason
        dataset_outcomes[dataset] = {
            "status": outcome,
            "reason": reason,
            "item_count": len(candidate_items),
            "pruned": dict(pruned_counts[dataset]),
        }
        LOGGER.info(
            "Dataset %s: %s items=%d pruned=%d reason=%s",
            dataset,
            outcome,
            len(candidate_items),
            sum(pruned_counts[dataset].values()),
            reason,
        )

    for dataset in ("builds", "hackathons"):
        dataset_outcomes[dataset] = {
            "status": "skipped",
            "reason": "Not registered in Phase 2; existing file left untouched.",
        }
        LOGGER.info("Dataset %s: skipped (not registered in Phase 2)", dataset)

    counts, on_disk = _actual_counts(target_dir)
    metadata = _new_metadata(
        previous_metadata,
        source_summaries,
        counts,
        on_disk,
        now=run_at,
        data_changed=data_changed,
        provenance=metadata_provenance,
    )
    metadata_valid, metadata_reason = validate_metadata(metadata)
    if not metadata_valid:
        metadata_outcome = {
            "status": "refused",
            "reason": metadata_reason or "Metadata validation failed.",
        }
        metadata_refused = True
    elif dry_run:
        metadata_outcome = {"status": "skipped", "reason": "Dry run: metadata was not written."}
        metadata_refused = False
    else:
        published_metadata = storage.publish_metadata(metadata, target_dir / "metadata.json")
        if published_metadata.published:
            metadata_outcome = {"status": "published", "reason": published_metadata.reason}
            metadata_refused = False
        elif published_metadata.unchanged:
            metadata_outcome = {"status": "unchanged", "reason": published_metadata.reason}
            metadata_refused = False
        else:
            metadata_outcome = {"status": "refused", "reason": published_metadata.reason}
            metadata_refused = True

    dataset_refused = any(
        outcome["status"] == "refused" for outcome in dataset_outcomes.values()
    )
    summary = {
        "started_at": run_iso,
        "dry_run": dry_run,
        "strict": strict,
        "sources": per_source,
        "datasets": dataset_outcomes,
        "metadata": metadata_outcome,
        "pruned": pruned_counts,
        "data_changed": data_changed,
        "counts": counts,
    }
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))

    if dataset_refused or metadata_refused:
        return 1, summary
    if strict and source_errors:
        return 3, summary
    return 0, summary


def _parse_source_ids(value: str) -> list[str]:
    values = [part.strip() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise argparse.ArgumentTypeError("sources must be a comma-separated list of IDs.")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIR)
    parser.add_argument("--sources", type=_parse_source_ids, default=None, metavar="ID,ID,...")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        code, _ = asyncio.run(run_ingestion(
            args.data_dir,
            sources=args.sources,
            dry_run=args.dry_run,
            strict=args.strict,
            summary_json=args.summary_json,
        ))
        return code
    except UsageError as error:
        print(sanitize_error(error), file=sys.stderr)
        return 2
    except Exception as error:
        LOGGER.error("Ingestion failed: %s", sanitize_error(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
