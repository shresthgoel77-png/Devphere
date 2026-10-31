import pytest

from models import DATASET_NAMES
import storage
from validation import validate_metadata


def valid_metadata():
    return {
        "schemaVersion": 1,
        "dataOrigin": "seed",
        "lastUpdatedIso": "2026-10-30T12:00:00Z",
        "lastAttemptIso": None,
        "counts": {name: 0 for name in DATASET_NAMES},
        "sources": {},
    }


@pytest.mark.parametrize("provenance", [
    {
        "source": "Synthetic benchmark",
        "sourceUrl": "https://example.invalid/benchmark",
        "snapshotIso": "2026-10-29T00:00:00Z",
        "fetchedIso": "2026-10-30T12:00:00Z",
    },
    None,
    pytest.param("absent", id="absent"),
])
def test_publish_metadata_preserves_only_valid_non_null_benchmark_provenance(
    tmp_path, provenance
):
    metadata = valid_metadata()
    if provenance == "absent":
        metadata.pop("benchmarks", None)
    else:
        metadata["benchmarks"] = provenance

    assert validate_metadata(metadata) == (True, None)
    result = storage.publish_metadata(metadata, tmp_path / "metadata.json")

    assert result.published
    published, status = storage.load_published(tmp_path / "metadata.json")
    assert status == "ok"
    assert validate_metadata(published) == (True, None)
    if isinstance(provenance, dict):
        assert published["benchmarks"] == provenance
    else:
        assert "benchmarks" not in published
