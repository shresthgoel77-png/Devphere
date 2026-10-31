"""Atomic, fail-closed publication of static JSON; no fetching or retention policy.

Item order is supplied by callers: sort it deterministically before publication.
Validation may clean optional URLs, but any rejected item refuses the entire write.
This module assumes a single writer per published path (as in one Actions job).
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from models import DATASET_NAMES, SCHEMA_VERSION, PublishOutcome, sanitize_error
from validation import validate_dataset_items, validate_envelope, validate_metadata

_MAX_METADATA_BYTES = 16 * 1024


def _reject_constant(_: str) -> None:
    raise ValueError("Non-JSON numeric constant.")


def _parse(text: str) -> object:
    return json.loads(text, parse_constant=_reject_constant)


def _valid_dataset(dataset: str, obj: object) -> tuple[bool, str | None]:
    ok, error = validate_envelope(dataset, obj)
    if not ok:
        return ok, error
    report = validate_dataset_items(dataset, obj["items"])
    if len(report.accepted) != len(obj["items"]):
        return False, "Candidate contains rejected records; publication refused."
    return True, None


def _load_dataset(path: Path, dataset: str) -> tuple[dict | None, str]:
    try:
        obj = _parse(path.read_text(encoding="utf-8"))
        return (obj, "ok") if _valid_dataset(dataset, obj)[0] else (None, "invalid")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, TypeError, RecursionError):
        return None, "invalid"


def load_published(path: str | Path) -> tuple[dict | None, str]:
    """Strict last-known-good check, including every item, not just the envelope.

    Canonical filenames determine the dataset. For other filenames, accept only
    an envelope whose items all validate against one of the four contracts.
    Metadata is recognized by its lastUpdatedIso field (or canonical filename).
    """
    try:
        path = Path(path)
        obj = _parse(path.read_text(encoding="utf-8"))
        if path.stem == "metadata" or isinstance(obj, dict) and "lastUpdatedIso" in obj:
            return (obj, "ok") if validate_metadata(obj)[0] else (None, "invalid")
        datasets = (path.stem,) if path.stem in DATASET_NAMES else DATASET_NAMES
        return (obj, "ok") if any(_valid_dataset(d, obj)[0] for d in datasets) else (None, "invalid")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, TypeError, RecursionError):
        return None, "invalid"


def _serialize(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False, allow_nan=False) + "\n"


def serialize_envelope(items: list[dict], data_origin: str, generated_at_iso: str) -> str:
    """Stable envelope key order; caller must supply deterministically sorted items."""
    return _serialize({"schemaVersion": SCHEMA_VERSION, "dataOrigin": data_origin,
                       "generatedAtIso": generated_at_iso, "items": items})


def _atomic_publish(dataset: str, path: Path, content: str, item_count: int,
                    validator: Callable[[object], tuple[bool, str | None]]) -> PublishOutcome:
    payload = content.encode("utf-8")
    outcome = PublishOutcome(dataset, False, "Publication failed.", str(path), len(payload), item_count)
    temp_path: Path | None = None
    try:
        # Binary mode avoids Windows newline translation; UTF-8 bytes match size/no-op checks.
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp") as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_bytes = temp_path.read_bytes()
        ok, error = validator(_parse(temp_bytes.decode("utf-8")))
        if not ok or temp_bytes != payload:
            outcome.reason = error or "Temporary content differs from the validated candidate."
        else:
            try:
                previous_bytes = path.read_bytes()
            except FileNotFoundError:
                previous_bytes = None
            if previous_bytes == payload:
                outcome.reason = "Content is unchanged."
                outcome.unchanged = True
            else:
                os.replace(temp_path, path)
                outcome.published = True
                outcome.reason = "Published atomically."
    except (OSError, ValueError, TypeError, RecursionError) as error:
        outcome.reason = "Publication refused: " + sanitize_error(error)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as error:
                # A filesystem refusing deletion cannot be forced to clean up safely.
                outcome.reason += " Temporary cleanup failed: " + sanitize_error(error)
    return outcome


def publish_dataset(dataset: str, items: object, data_origin: str, generated_at_iso: str,
                    path: str | Path, *, max_items: int, max_bytes: int,
                    allow_empty: bool = False) -> PublishOutcome:
    outcome = PublishOutcome(dataset, False, "Candidate is invalid.", str(path))
    try:
        candidate = {"schemaVersion": SCHEMA_VERSION, "dataOrigin": data_origin,
                     "generatedAtIso": generated_at_iso, "items": items}
        ok, error = _valid_dataset(dataset, candidate)
        if not ok:
            outcome.reason = error
            return outcome
        outcome.item_count = len(items)
        if (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0
                or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0):
            outcome.reason = "Publication limits must be non-negative integers."
            return outcome
        if len(items) > max_items:
            outcome.reason = "Candidate exceeds max_items."
            return outcome
        cleaned = validate_dataset_items(dataset, items).accepted
        content = serialize_envelope(cleaned, data_origin, generated_at_iso)
        outcome.bytes = len(content.encode("utf-8"))
        if outcome.bytes > max_bytes:
            outcome.reason = "Candidate exceeds max_bytes."
            return outcome
        target = Path(path)
        previous, status = _load_dataset(target, dataset)
        if not items and allow_empty is not True:
            if status == "invalid":
                outcome.reason = "Empty candidate cannot replace an invalid existing dataset."
                return outcome
            if status == "ok" and previous["items"]:
                outcome.reason = "Empty candidate cannot replace valid non-empty data without allow_empty=True."
                return outcome
        return _atomic_publish(dataset, target, content, len(cleaned), lambda obj: _valid_dataset(dataset, obj))
    except (OSError, ValueError, TypeError, RecursionError) as error:
        outcome.reason = "Publication refused: " + sanitize_error(error)
        return outcome


def publish_metadata(metadata: object, path: str | Path) -> PublishOutcome:
    outcome = PublishOutcome("metadata", False, "Metadata is invalid.", str(path))
    try:
        candidate = metadata
        if isinstance(metadata, dict):
            candidate = dict(metadata)
            sources = metadata.get("sources")
            if isinstance(sources, dict):
                sanitized_sources = {}
                for source_id, entry in sources.items():
                    if isinstance(entry, dict):
                        entry = dict(entry)
                        summary = entry.get("errorSummary")
                        if isinstance(summary, str):
                            entry["errorSummary"] = sanitize_error(summary)
                    sanitized_sources[source_id] = entry
                candidate["sources"] = sanitized_sources

        ok, error = validate_metadata(candidate)
        if not ok:
            outcome.reason = error
            return outcome
        # Strip extras just as parseMetadata does, retaining source insertion order.
        fields = ("status", "lastSuccessIso", "lastAttemptIso", "recordsFetched", "recordsAccepted", "errorSummary")
        cleaned = {"schemaVersion": SCHEMA_VERSION, "dataOrigin": candidate["dataOrigin"],
                   "lastUpdatedIso": candidate["lastUpdatedIso"], "lastAttemptIso": candidate["lastAttemptIso"],
                   "counts": {name: candidate["counts"][name] for name in DATASET_NAMES},
                   "sources": {name: {f: entry[f] for f in fields} for name, entry in candidate["sources"].items()}}
        if candidate.get("benchmarks") is not None:
            cleaned["benchmarks"] = candidate["benchmarks"]
        content = _serialize(cleaned)
        outcome.bytes = len(content.encode("utf-8"))
        if outcome.bytes > _MAX_METADATA_BYTES:
            outcome.reason = "Metadata exceeds max_bytes."
            return outcome
        return _atomic_publish("metadata", Path(path), content, 0, validate_metadata)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        outcome.reason = "Publication refused: " + sanitize_error(error)
        return outcome


def merge_with_previous(previous_items: list[dict], new_items: list[dict], *, key: str = "id") -> list[dict]:
    """New records override previous ones; retain missing old records, sort by key.

    Keys must be non-empty strings. Last occurrence within either input wins;
    new input wins across inputs. Return deep copies without mutating inputs.
    Invalid merge keys raise ValueError rather than silently losing records.
    """
    merged: dict[str, dict] = {}
    for record in [*previous_items, *new_items]:
        if not isinstance(record, dict) or not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError("Merge records require a non-empty string key.")
        merged[record[key]] = record
    return [deepcopy(merged[value]) for value in sorted(merged)]
