from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from models import DATASET_NAMES
import storage
from validation import validate_dataset_items, validate_envelope, validate_metadata

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline_and_no_temp_leaks(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in contract tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    yield
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.fixture
def seeds():
    return {d: json.loads((ROOT / f"public/data/{d}.json").read_text(encoding="utf-8"))
            for d in (*DATASET_NAMES, "metadata")}


def publish(kind, data, path, **options):
    if kind == "metadata":
        return storage.publish_metadata(data, path)
    return storage.publish_dataset(kind, data["items"], data["dataOrigin"], data["generatedAtIso"], path,
                                   **{"max_items": 100, "max_bytes": 1_000_000, **options})


@pytest.mark.parametrize("kind", (*DATASET_NAMES, "metadata"))
def test_publish_success_valid_json_no_temp_leaks(seeds, tmp_path, kind):
    path = tmp_path / f"{kind}.json"
    data = seeds[kind]
    before = deepcopy(data)
    result = publish(kind, data, path)
    assert result.published and not result.unchanged
    assert result.dataset == kind and result.path == str(path)
    assert result.bytes == len(path.read_bytes())
    assert result.item_count == (0 if kind == "metadata" else len(data["items"]))
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert data == before
    assert storage.load_published(path) == (data, "ok")
    assert path.read_bytes().endswith(b"\n") and b"\r\n" not in path.read_bytes()


@pytest.mark.parametrize("kind", ["news", "metadata"])
def test_byte_identical_no_rewrite_even_if_temp_is_used(seeds, tmp_path, monkeypatch, kind):
    path = tmp_path / f"{kind}.json"
    assert publish(kind, seeds[kind], path).published
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    def forbidden(*args):
        pytest.fail("Identical content must not call os.replace")
    monkeypatch.setattr(storage.os, "replace", forbidden)
    result = publish(kind, seeds[kind], path)
    assert not result.published and result.unchanged
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("kind", ["news", "metadata"])
def test_invalid_candidate_preserves_existing_bytes(seeds, tmp_path, kind):
    path = tmp_path / f"{kind}.json"
    assert publish(kind, seeds[kind], path).published
    before = path.read_bytes()
    bad = deepcopy(seeds[kind])
    if kind == "metadata":
        bad["counts"]["news"] = -1
    else:
        bad["items"][0]["source"] = "bad-enum"
    result = publish(kind, bad, path)
    assert not result.published and not result.unchanged
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["news", "metadata"])
@pytest.mark.parametrize("failure", ["replace", "fsync", "create", "reread", "revalidate"])
def test_io_and_revalidation_failures_preserve_last_good(seeds, tmp_path, monkeypatch, kind, failure):
    path = tmp_path / f"{kind}.json"
    assert publish(kind, seeds[kind], path).published
    before = path.read_bytes()
    data = deepcopy(seeds[kind])
    if kind == "metadata":
        data["counts"]["news"] += 1
    else:
        data["items"][0]["title"] = "Changed"
    def fail(*args, **kwargs):
        if failure == "replace":
            assert Path(args[0]).parent == path.parent  # SAME directory, closed file on Windows.
            assert Path(args[0]).suffix == ".tmp"
        raise OSError(r"C:\private\secret.txt token=never-echo-this")
    if failure in ("replace", "fsync"):
        monkeypatch.setattr(storage.os, failure, fail)
    elif failure == "create":
        monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", fail)
    else:
        read_bytes = Path.read_bytes
        def broken_read(file):
            if file.suffix == ".tmp":
                if failure == "reread":
                    return b"{"
                # Well-formed JSON with a broken contract must fail revalidation.
                return b'{"schemaVersion":99}'
            return read_bytes(file)
        monkeypatch.setattr(Path, "read_bytes", broken_read)
    result = publish(kind, data, path)
    assert not result.published and not result.unchanged
    assert all(word not in result.reason for word in ("private", "secret", "never-echo"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["news", "metadata"])
def test_corrupt_existing_reported_invalid_and_replaced(seeds, tmp_path, kind):
    path = tmp_path / f"{kind}.json"
    assert storage.load_published(path) == (None, "missing")
    path.write_text("{", encoding="utf-8")
    assert storage.load_published(path) == (None, "invalid")
    assert publish(kind, seeds[kind], path).published
    assert storage.load_published(path)[1] == "ok"


def test_load_invalid_json_contract_and_garbage_paths(tmp_path):
    path = tmp_path / "news.json"
    for text in ("null", "[]", '"garbage"', '{"schemaVersion":NaN}', '{"schemaVersion":1}'):
        path.write_text(text, encoding="utf-8")
        assert storage.load_published(path) == (None, "invalid")
    assert storage.load_published(None) == (None, "invalid")
    assert storage.load_published(tmp_path) == (None, "invalid")


@pytest.mark.parametrize("dataset", DATASET_NAMES)
def test_empty_over_nonempty_refused_unless_explicit(seeds, tmp_path, dataset):
    path = tmp_path / f"{dataset}.json"
    assert publish(dataset, seeds[dataset], path).published
    before = path.read_bytes()
    empty = {**seeds[dataset], "items": []}
    result = publish(dataset, empty, path)
    assert not result.published and "allow_empty=True" in result.reason
    assert path.read_bytes() == before
    assert publish(dataset, empty, path, allow_empty=True).published
    assert storage.load_published(path)[0]["items"] == []


def test_empty_allowed_for_new_file_and_preexisting_empty(seeds, tmp_path):
    data = {**seeds["news"], "items": []}
    path = tmp_path / "arbitrary-name.json"
    assert publish("news", data, path).published
    assert publish("news", data, path).unchanged
    assert storage.load_published(path) == (data, "ok")


@pytest.mark.parametrize("options,reason", [
    ({"max_items": 1}, "max_items"), ({"max_bytes": 1}, "max_bytes"),
    ({"max_items": True}, "limits"), ({"max_bytes": -1}, "limits"),
])
def test_limits_refuse_before_touching_file(seeds, tmp_path, options, reason):
    path = tmp_path / "news.json"
    assert publish("news", seeds["news"], path).published
    before = path.read_bytes()
    result = publish("news", seeds["news"], path, **options)
    assert not result.published and reason in result.reason
    assert path.read_bytes() == before


def test_limits_measure_encoded_utf8_bytes_and_allow_exact_boundaries(seeds, tmp_path):
    data = deepcopy(seeds["news"])
    data["items"][0]["title"] = "Café \U0001f331"
    content = storage.serialize_envelope(data["items"], data["dataOrigin"], data["generatedAtIso"])
    path = tmp_path / "news.json"
    assert "Café" in content and len(content.encode("utf-8")) > len(content)
    assert not publish("news", data, path, max_bytes=len(content)).published
    assert publish("news", data, path, max_items=10, max_bytes=len(content.encode("utf-8"))).published
    assert path.read_bytes() == content.encode("utf-8")


def test_bad_envelope_and_bad_items_refuse_without_writing(seeds, tmp_path):
    path = tmp_path / "news.json"
    assert publish("news", seeds["news"], path).published
    before = path.read_bytes()
    for patch in ({"dataOrigin": "fake"}, {"generatedAtIso": "invalid"}, {"items": None},
                  {"items": [None]}, {"items": [seeds["news"]["items"][0]] * 2}):
        result = publish("news", {**seeds["news"], **patch}, path)
        assert not result.published and path.read_bytes() == before
    assert not publish("unknown", seeds["news"], path).published


def test_optional_url_diagnostics_do_not_prevent_safe_publication(seeds, tmp_path):
    data = deepcopy(seeds["news"])
    data["items"][0]["thumbnailUrl"] = "javascript:alert(1)"
    data["items"][0]["extra"] = "ignored"
    path = tmp_path / "news.json"
    assert publish("news", data, path).published
    published, status = storage.load_published(path)
    assert status == "ok" and "thumbnailUrl" not in published["items"][0]
    assert "extra" not in published["items"][0]
    assert validate_envelope("news", published)[0]
    assert not validate_dataset_items("news", published["items"]).rejected


def test_metadata_unknown_fields_stripped_input_untouched(seeds, tmp_path):
    metadata = {**seeds["metadata"], "extra": "ignored"}
    metadata["counts"]["unknown"] = 3
    metadata["sources"] = {"reddit": {"status": "failed", "lastSuccessIso": None,
        "lastAttemptIso": None, "recordsFetched": None, "recordsAccepted": None,
        "errorSummary": "Sanitized upstream failure.", "extra": []}}
    before = deepcopy(metadata)
    path = tmp_path / "metadata.json"
    assert storage.publish_metadata(metadata, path).published
    output = json.loads(path.read_text(encoding="utf-8"))
    assert validate_metadata(output)[0]
    assert "extra" not in output and "unknown" not in output["counts"]
    assert "extra" not in output["sources"]["reddit"]
    assert metadata == before


def test_metadata_error_summaries_are_sanitized_and_capped(seeds, tmp_path):
    metadata = deepcopy(seeds["metadata"])
    metadata["sources"] = {"reddit": {"status": "failed", "lastSuccessIso": None,
        "lastAttemptIso": None, "recordsFetched": None, "recordsAccepted": None,
        "errorSummary": "token=secret Traceback C:\\private\\source.py " + "x" * 1000}}
    before = deepcopy(metadata)
    path = tmp_path / "metadata.json"

    result = storage.publish_metadata(metadata, path)

    assert result.published
    summary = json.loads(path.read_text(encoding="utf-8"))["sources"]["reddit"]["errorSummary"]
    assert summary == "operation failure; untrusted error details withheld."
    assert len(summary) <= 300
    assert all(value not in summary for value in ("secret", "Traceback", "private", "source.py"))
    assert metadata == before


def test_oversized_metadata_preserves_last_good_file(seeds, tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    assert storage.publish_metadata(seeds["metadata"], path).published
    before = path.read_bytes()
    changed = deepcopy(seeds["metadata"])
    changed["counts"]["news"] += 1
    monkeypatch.setattr(storage, "_MAX_METADATA_BYTES", 32)

    result = storage.publish_metadata(changed, path)

    assert not result.published and not result.unchanged
    assert "max_bytes" in result.reason
    assert result.bytes > 32
    assert path.read_bytes() == before


def test_missing_parent_failure_never_raises(seeds, tmp_path):
    path = tmp_path / "absent" / "news.json"
    assert not publish("news", seeds["news"], path).published
    assert not storage.publish_metadata(seeds["metadata"], path).published


def test_merge_retains_old_records_overrides_and_sorts():
    old = [{"id": "z", "tags": ["keep"]}, {"id": "a", "value": 1}]
    new = [{"id": "a", "value": 2}, {"id": "b", "value": 3}]
    before = deepcopy((old, new))
    assert storage.merge_with_previous(old, []) == [old[1], old[0]]
    merged = storage.merge_with_previous(old, new)
    assert [r["id"] for r in merged] == ["a", "b", "z"]
    assert merged[0]["value"] == 2
    merged[-1]["tags"].append("changed")
    assert (old, new) == before
    assert storage.merge_with_previous([], []) == []
    assert storage.merge_with_previous([{"slug": "x", "v": 1}], [{"slug": "x", "v": 2}], key="slug") == [{"slug": "x", "v": 2}]
    assert storage.merge_with_previous([], [{"id": "x", "v": 1}, {"id": "x", "v": 2}]) == [{"id": "x", "v": 2}]


@pytest.mark.parametrize("bad", [{}, {"id": None}, {"id": []}, None])
def test_merge_bad_keys_fail_explicitly(bad):
    with pytest.raises(ValueError):
        storage.merge_with_previous([bad], [])
