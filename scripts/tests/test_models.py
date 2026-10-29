import re
import socket
from pathlib import Path

import pytest

import models

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "src/types/schema.ts").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in contract tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.mark.parametrize("name", [
    "DATA_ORIGINS", "DATASET_NAMES", "NEWS_SOURCES", "MODEL_MODALITIES",
    "BUILD_PLATFORMS", "HACKATHON_PLATFORMS", "HACKATHON_MODES", "SOURCE_IDS", "SOURCE_STATUSES",
])
def test_enum_constants_match_canonical_typescript(name):
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", SCHEMA, re.S)
    assert match is not None
    assert getattr(models, name) == tuple(re.findall(r'"([^\"]+)"', match[1]))


@pytest.mark.parametrize("name,constant", [
    ("DataOrigin", "DATA_ORIGINS"), ("DatasetName", "DATASET_NAMES"),
    ("SourceId", "SOURCE_IDS"), ("SourceStatus", "SOURCE_STATUSES"),
])
def test_type_unions_match_python(name, constant):
    match = re.search(rf"export type {name}\s*=\s*(.*?);", SCHEMA, re.S)
    assert match is not None
    assert tuple(re.findall(r'"([^\"]+)"', match[1])) == getattr(models, constant)


@pytest.mark.parametrize("dataset,interface", [
    ("news", "NewsItem"), ("models", "AIModelItem"),
    ("builds", "TechBuildItem"), ("hackathons", "HackathonItem"),
])
def test_field_tables_match_ts_interfaces(dataset, interface):
    match = re.search(rf"export interface {interface} \{{(.*?)\n\}}", SCHEMA, re.S)
    fields = re.findall(r"^\s*(\w+)(\?)?:\s*(.+);$", match[1], re.M)
    specs = models.DATASET_FIELDS[dataset]
    assert [s.name for s in specs] == [f[0] for f in fields]
    kinds = {"string": "string", "nullable_summary": "string | null",
             "url": "string", "datetime": "string", "number": "number",
             "nullable_number": "number | null", "boolean": "boolean", "strings": "string[]"}
    for spec, (_, optional, ts_type) in zip(specs, fields, strict=True):
        assert spec.required == (not optional)
        if spec.kind == "enum":
            assert spec.choices == tuple(re.findall(r'"([^\"]+)"', ts_type))
        else:
            assert kinds[spec.kind] == ts_type
    assert models.SCHEMA_VERSION == int(re.search(r"SCHEMA_VERSION = (\d+)", SCHEMA)[1])


@pytest.mark.parametrize("error", [
    "token=super-secret /private/file.py Traceback " + "x" * 1000,
    OSError(r"C:\private\file.py api_key=secret"),
    TimeoutError("https://user:password@example.com"),
])
def test_failure_result_never_echoes_untrusted_details(error):
    result = models.SourceResult.failure("hackernews", error)
    assert result.status == "failed"
    assert result.records == [] and result.fetched == 0
    assert 0 < len(result.error_summary) <= 300
    assert all(word not in result.error_summary for word in ("secret", "private", "Traceback", "password"))
    assert result.started_iso and result.finished_iso
    result.records.append({"id": "test"})
    assert models.SourceResult.failure("reddit", error).records == []
