from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess

import pytest

from models import DATASET_FIELDS, DATASET_NAMES, SOURCE_STATUSES
from validation import (
    is_safe_external_url, parse_iso_datetime, safe_external_url_or_none,
    validate_dataset_items, validate_envelope, validate_metadata, validate_record,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in contract tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def seeds():
    return {d: json.loads((ROOT / f"public/data/{d}.json").read_text(encoding="utf-8"))
            for d in (*DATASET_NAMES, "metadata")}


@pytest.mark.parametrize("dataset", DATASET_NAMES)
def test_seed_dataset_has_zero_rejections(dataset, seeds):
    seed = seeds[dataset]
    assert validate_envelope(dataset, seed) == (True, None)
    report = validate_dataset_items(dataset, seed["items"])
    assert report.rejected == [] and report.dropped_optional == []
    assert len(report.accepted) == seeds["metadata"]["counts"][dataset] == 10
    assert report.accepted == seed["items"]


def test_metadata_seed(seeds):
    assert validate_metadata(seeds["metadata"]) == (True, None)


def test_nullable_news_metrics_and_summary(seeds):
    record = seeds["news"]["items"][0]
    unknown = {**record, "upvotes": None, "commentsCount": None, "summary": None}
    before = deepcopy(unknown)
    report = validate_record("news", unknown)
    assert report.accepted[0] == unknown
    report.accepted[0]["tags"].append("local-only")
    assert unknown == before

    verified_zero = {**record, "upvotes": 0, "commentsCount": 0}
    accepted_zero = validate_record("news", verified_zero).accepted[0]
    assert accepted_zero["upvotes"] == 0
    assert accepted_zero["commentsCount"] == 0

    for field in ("upvotes", "commentsCount"):
        missing = {**record}
        del missing[field]
        assert not validate_record("news", missing).accepted
        assert not validate_record("news", {**record, field: float("nan")}).accepted

    for summary in ("", "   "):
        report = validate_record("news", {**record, "summary": summary})
        assert not report.accepted
        assert report.rejected[0].reasons == [
            "summary must be null or a non-empty string."
        ]


@pytest.mark.parametrize("benchmarks", [
    {"source": "Fictional benchmark source", "sourceUrl": "https://example.invalid/benchmark",
     "snapshotIso": "2026-01-01T00:00:00Z", "fetchedIso": "2026-01-02T00:00:00Z"},
    {"source": "source", "sourceUrl": "javascript:alert(1)",
     "snapshotIso": None, "fetchedIso": "2026-01-02T00:00:00Z"},
    {"source": "source", "sourceUrl": "https://example.invalid/benchmark",
     "snapshotIso": "not-a-date", "fetchedIso": "2026-01-02T00:00:00Z"},
    {"source": "source", "sourceUrl": "https://example.invalid/benchmark",
     "snapshotIso": None, "fetchedIso": "not-a-date"},
    {"source": "   ", "sourceUrl": "https://example.invalid/benchmark",
     "snapshotIso": None, "fetchedIso": "2026-01-02T00:00:00Z"},
    [],
])
def test_benchmark_provenance(seeds, benchmarks):
    metadata = seeds["metadata"]
    before = deepcopy(metadata)
    assert validate_metadata(metadata) == (True, None)
    assert metadata == before
    candidate = {**metadata, "benchmarks": benchmarks}
    is_valid = (
        isinstance(benchmarks, dict)
        and benchmarks.get("source") == "Fictional benchmark source"
    )
    assert validate_metadata(candidate)[0] is is_valid
    if not is_valid:
        assert validate_metadata(candidate)[1] == "Metadata benchmark provenance is invalid."


def test_null_benchmark_provenance_is_valid(seeds):
    assert validate_metadata({**seeds["metadata"], "benchmarks": None}) == (True, None)


@pytest.mark.parametrize("field,value,reason", [
    ("title", None, "title must be a non-empty string."),
    ("source", "NotARealSource", "source must be one of: HackerNews, Reddit, Dev.to, HuggingFace, YouTube."),
    ("upvotes", float("nan"), "upvotes must be null or a finite number."),
    ("upvotes", float("inf"), "upvotes must be null or a finite number."),
    ("upvotes", -float("inf"), "upvotes must be null or a finite number."),
    ("upvotes", True, "upvotes must be null or a finite number."),
    ("url", "javascript:alert(1)", "url must be a safe HTTPS URL."),
    ("publishedAtIso", "not-a-date", "publishedAtIso must be a valid ISO date-time string."),
    ("publishedAtIso", "2026-02-30T12:00:00Z", "publishedAtIso must be a valid ISO date-time string."),
    ("tags", ["valid", 42], "tags must be an array of strings."),
])
def test_ts_invalid_records_preserve_valid_sibling(seeds, field, value, reason):
    good = seeds["news"]["items"][0]
    bad = {**good, "id": "bad", field: value}
    result = validate_dataset_items("news", [good, bad])
    assert result.accepted == [good]
    assert result.rejected[0].index == 1 and reason in result.rejected[0].reasons


@pytest.mark.parametrize("dataset", DATASET_NAMES)
def test_every_required_field_is_required(dataset, seeds):
    for spec in DATASET_FIELDS[dataset]:
        if spec.required:
            record = deepcopy(seeds[dataset]["items"][0])
            record.pop(spec.name)
            report = validate_record(dataset, record)
            assert not report.accepted, spec.name
            assert any(spec.name in r for r in report.rejected[0].reasons)


@pytest.mark.parametrize("dataset,field", [
    ("news", "thumbnailUrl"), ("builds", "repoUrl"), ("builds", "liveDemoUrl"),
    ("builds", "thumbnailUrl"), ("hackathons", "bannerUrl"),
])
@pytest.mark.parametrize("value", ["javascript:alert(1)", None, [], 42])
def test_optional_unsafe_url_dropped_and_record_is_also_in_rejected(seeds, dataset, field, value):
    record = {**seeds[dataset]["items"][0], field: value}
    before = deepcopy(record)
    report = validate_record(dataset, record)
    assert len(report.accepted) == 1 and field not in report.accepted[0]
    assert len(report.rejected) == len(report.dropped_optional) == 1
    assert report.rejected[0].reasons == [f"{field} was dropped because it is not a safe HTTPS URL."]
    assert record == before


@pytest.mark.parametrize("dataset,field,value", [
    ("builds", "dailyVelocity", None), ("builds", "isWinner", 1),
    ("builds", "hackathonName", "  "), ("hackathons", "participantsCount", False),
    ("models", "isOpenWeights", 0), ("models", "arenaElo", True),
    ("models", "codingElo", float("inf")), ("models", "inputCostPer1M", "0"),
    ("models", "outputCostPer1M", []), ("hackathons", "prizeValueNormalizedUsd", False),
])
def test_invalid_optional_non_urls_and_nullable_numbers_reject(seeds, dataset, field, value):
    assert not validate_record(dataset, {**seeds[dataset]["items"][0], field: value}).accepted


def test_duplicate_first_valid_occurrence_and_optional_diagnostics(seeds):
    record = seeds["news"]["items"][0]
    unsafe = {**record, "thumbnailUrl": "javascript:alert(1)"}
    report = validate_dataset_items("news", [unsafe, record])
    assert len(report.accepted) == 1 and "thumbnailUrl" not in report.accepted[0]
    assert len(report.rejected) == 2
    assert report.rejected[1].reasons == ["duplicate id; first occurrence was kept."]
    report = validate_dataset_items("news", [{**record, "source": "bad"}, record])
    assert report.accepted == [record]  # A bad first occurrence never reserves the ID.


def test_null_and_zero_are_distinct_and_no_input_mutation(seeds):
    before = deepcopy(seeds)
    result = validate_dataset_items("models", seeds["models"]["items"])
    for field in ("arenaElo", "codingElo", "inputCostPer1M", "outputCostPer1M"):
        assert result.accepted[0][field] is None
    assert result.accepted[3]["inputCostPer1M"] == 0
    assert result.accepted[3]["outputCostPer1M"] == 0
    result.accepted[0]["categoryTags"].append("changed")
    assert seeds == before


def test_unknown_fields_stripped_whitespace_and_finite_negative_values_preserved(seeds):
    record = {**seeds["news"]["items"][0], "title": "  title  ", "upvotes": -0.5, "extra": {"nested": []}}
    accepted = validate_record("news", record).accepted[0]
    assert "extra" not in accepted and accepted["title"] == "  title  " and accepted["upvotes"] == -0.5
    assert validate_record("news", {**record, "tags": [""]}).accepted


@pytest.mark.parametrize("garbage", [None, "garbage", [], [None], 42, True, {}, {"nested": {"items": []}}, {"id": []}])
def test_garbage_never_raises(garbage):
    for dataset in (*DATASET_NAMES, None, [], {}):
        assert not validate_record(dataset, garbage).accepted
        validate_dataset_items(dataset, garbage)
        assert not validate_envelope(dataset, garbage)[0]
    assert not validate_metadata(garbage)[0]
    assert parse_iso_datetime(garbage) is None
    assert safe_external_url_or_none(garbage) is None


def test_empty_items_valid_and_bad_envelopes(seeds):
    assert validate_dataset_items("news", []).accepted == []
    assert validate_dataset_items("news", []).rejected == []
    assert validate_envelope("news", {**seeds["news"], "items": []}) == (True, None)
    for patch in ({"schemaVersion": 99}, {"schemaVersion": True}, {"items": {}},
                  {"dataOrigin": "fake"}, {"generatedAtIso": "2026-01-01"}):
        assert not validate_envelope("news", {**seeds["news"], **patch})[0]


@pytest.mark.parametrize("value", [
    "2026-01-01T00:00:00Z", "2024-02-29T12:30:59.123456789+05:30",
    "2026-10-27T24:00:00Z", "0001-01-01T00:00:00-23:59",
    "9999-12-31T24:00:00Z",
])
def test_valid_aware_iso(value):
    assert parse_iso_datetime(value).tzinfo is not None


@pytest.mark.parametrize("value", [
    "2026-01-01", "2026-01-01T00:00:00", "2026-02-30T00:00:00Z", "2026-02-29T00:00:00Z",
    "0000-01-01T00:00:00Z", "2026-01-01T00:00:00+01:60", "2026-01-01T24:00:01Z",
    "2026-01-01T24:00:00.01Z", "2026-01-01T00:00:60Z", "2026-01-01t00:00:00z",
])
def test_invalid_iso(value):
    assert parse_iso_datetime(value) is None


# Every rejection in tests/urls.test.ts, plus hostname/port/special-scheme cases.
UNSAFE_URLS = [
    "javascript:alert(1)", "data:text/html,example", "http://example.com", "file:///tmp/example",
    "blob:https://example.com/id", "https://user:password@example.com/path", "", " https://example.com",
    "https://example.com ", "//example.com/path", 42, None, "https://example.com/" + "a" * 2048,
    "https://", "https://example.com:65536", "https://bad host/", "https://999.0.0.1/", "https://%zz/",
    "https://\u0627\u200c\u0647.com/", "https://\u0647\u200d\u0627.com/",
]
SAFE_URLS = ["https://example.com/path?q=1", "https://example.com/path", "HTTPS://example.com",
             "https:example.com", "https://[::1]/", "https://127.1/", "https://example.com:443/",
             "https://@example.com", "https://exa\tmple.com/", "https:\\example.com/path",
             "https://a..b", "https://.example.com", "https://example.com..", "https://foo.+1",
             "https://foo.-1", "https://" + "a" * 65 + ".com", "\x01https://example.com",
             "https://example.com\x01", "https://\u00ad.com/", "https://\u034f.com/",
             "https://\u0d15\u0d4d\u200d.com/", "https://" + "a" * 65 + "\u00df.com/",
             "https://\u0646\u0627\u0645\u0647\u200c\u0627\u06cc.com/"]


@pytest.mark.parametrize("value", UNSAFE_URLS)
def test_url_rejections(value):
    assert not is_safe_external_url(value)
    assert safe_external_url_or_none(value) is None


@pytest.mark.parametrize("value", SAFE_URLS)
def test_safe_urls(value):
    assert is_safe_external_url(value)
    assert safe_external_url_or_none(value) == value


def test_url_table_matches_actual_ts_function_offline():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed only for the additional cross-language comparison")
    ts = (ROOT / "src/lib/urls.ts").read_text(encoding="utf-8").split("export function safeExternalUrlOrNull")[0]
    js = ts.replace("export function isSafeExternalUrl(value: unknown): value is string",
                    "function isSafeExternalUrl(value)")
    js += '\nprocess.stdout.write(JSON.stringify(JSON.parse(require("fs").readFileSync(0,"utf8")).map(isSafeExternalUrl)));'
    values = UNSAFE_URLS + SAFE_URLS
    result = subprocess.run([node, "-e", js], input=json.dumps(values), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [is_safe_external_url(value) for value in values]


def test_actual_typescript_dataset_and_metadata_parsers_offline(seeds):
    """Transpile the existing canonical files in memory; never edit JS or fetch."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed only for the additional cross-language comparison")
    source = "\n".join(re.sub(r'^import[\s\S]*?from\s+["\'][^"\']+["\'];\s*', "",
        (ROOT / path).read_text(encoding="utf-8"), flags=re.M)
        for path in ("src/types/schema.ts", "src/lib/urls.ts", "src/lib/data.ts"))
    cases = []
    for dataset in DATASET_NAMES:
        envelope = seeds[dataset]
        good = envelope["items"][0]
        cases.append([dataset, envelope])
        cases.append([dataset, {**envelope, "items": [good, good]}])
        for spec in DATASET_FIELDS[dataset]:
            missing = {k: v for k, v in good.items() if k != spec.name}
            variants = [missing] + [{**good, spec.name: v} for v in (None, [], True, 42, "")]
            for variant in variants:
                cases.append([dataset, {**envelope, "items": [variant]}])
    news_envelope = seeds["news"]
    news_item = news_envelope["items"][0]
    for timestamp in ("9999-12-31T24:00:00Z", "9999-12-31T24:00:01Z"):
        cases.append(["news", {**news_envelope, "items": [
            {**news_item, "publishedAtIso": timestamp},
        ]}])
    provenance = {
        "source": "Fictional benchmark source",
        "sourceUrl": "https://example.invalid/benchmark",
        "snapshotIso": None,
        "fetchedIso": "2026-01-02T00:00:00Z",
    }
    metadata_cases = [
        seeds["metadata"], None, {}, {**seeds["metadata"], "schemaVersion": 99},
        {**seeds["metadata"], "benchmarks": None},
        {**seeds["metadata"], "benchmarks": provenance},
        {**seeds["metadata"], "benchmarks": {**provenance, "sourceUrl": "javascript:alert(1)"}},
        {**seeds["metadata"], "benchmarks": {**provenance, "snapshotIso": "not-a-date"}},
        {**seeds["metadata"], "benchmarks": {**provenance, "fetchedIso": "not-a-date"}},
        {**seeds["metadata"], "benchmarks": {**provenance, "source": ""}},
        {**seeds["metadata"], "benchmarks": []},
    ]
    for status in SOURCE_STATUSES:
        metadata_cases.append({**seeds["metadata"], "sources": {"hackernews": {
            "status": status, "lastSuccessIso": None, "lastAttemptIso": None,
            "recordsFetched": -1, "recordsAccepted": 0, "errorSummary": None}}})
    js = '''
const fs = require("fs"), ts = require("typescript");
const input = JSON.parse(fs.readFileSync(0, "utf8")), mod = {exports:{}};
const output = ts.transpileModule(input.source, {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
new Function("exports", "require", output)(mod.exports, require);
process.stdout.write(JSON.stringify({datasets:input.cases.map(([d,e])=>mod.exports.parseDataset(d,e)),
 metadata:input.metadata.map(e=>mod.exports.parseMetadata(e))}));
'''
    output = subprocess.run([node, "-e", js], input=json.dumps({"source": source, "cases": cases,
        "metadata": metadata_cases}), text=True, encoding="utf-8", capture_output=True, check=True,
        cwd=ROOT, timeout=30)
    ts = json.loads(output.stdout)
    for (dataset, envelope), actual in zip(cases, ts["datasets"], strict=True):
        report = validate_dataset_items(dataset, envelope["items"])
        assert actual["status"] == "ok"
        assert actual["items"] == report.accepted
        assert [(r["index"], r["id"], r["reasons"]) for r in actual["rejected"]] == [
            (r.index, r.id, r.reasons) for r in report.rejected]
    for candidate, actual in zip(metadata_cases, ts["metadata"], strict=True):
        ok, error = validate_metadata(candidate)
        assert (actual["status"] == "ok") == ok
        assert actual["error"] == error


def test_end_of_year_midnight_rollover_matches_typescript():
    assert parse_iso_datetime("9999-12-31T24:00:00Z") is not None


def test_unicode_hostname_matches_typescript_and_invalid_joiners_remain_rejected():
    assert is_safe_external_url("https://\u0646\u0627\u0645\u0647\u200c\u0627\u06cc.com/")
    assert not is_safe_external_url("https://\u0627\u200c\u0647.com/")
    assert not is_safe_external_url("https://\u0647\u200d\u0627.com/")


@pytest.mark.parametrize("status", SOURCE_STATUSES)
def test_metadata_source_contract_including_ts_signed_counters(seeds, status):
    metadata = seeds["metadata"]
    entry = {"status": status, "lastSuccessIso": None, "lastAttemptIso": "2026-10-27T12:00:00Z",
             "recordsFetched": -1, "recordsAccepted": None, "errorSummary": ""}
    metadata["sources"] = {"hackernews": entry}
    assert validate_metadata(metadata) == (True, None)  # TS allows signed source counters.
    for field in entry:
        bad = deepcopy(metadata)
        del bad["sources"]["hackernews"][field]
        assert not validate_metadata(bad)[0]
    for field, value in (("status", "bogus"), ("lastSuccessIso", "bad"), ("lastAttemptIso", 0),
                         ("recordsFetched", True), ("recordsAccepted", 1.5), ("errorSummary", [])):
        bad = deepcopy(metadata)
        bad["sources"]["hackernews"][field] = value
        assert not validate_metadata(bad)[0]
    metadata["sources"] = {"unknown": entry}
    assert not validate_metadata(metadata)[0]


def test_metadata_error_summary_is_capped(seeds):
    metadata = seeds["metadata"]
    metadata["sources"] = {"hackernews": {
        "status": "failed", "lastSuccessIso": None, "lastAttemptIso": None,
        "recordsFetched": None, "recordsAccepted": None, "errorSummary": "x" * 301,
    }}
    assert not validate_metadata(metadata)[0]


@pytest.mark.parametrize("bad", [-1, True, 1.5, float("inf"), None, "0"])
def test_metadata_counts_non_negative_integer(seeds, bad):
    metadata = seeds["metadata"]
    metadata["counts"]["news"] = bad
    assert not validate_metadata(metadata)[0]


def test_metadata_missing_and_bad_top_level_fields(seeds):
    for field in seeds["metadata"]:
        candidate = deepcopy(seeds["metadata"])
        del candidate[field]
        assert not validate_metadata(candidate)[0]
    assert not validate_metadata({**seeds["metadata"], "schemaVersion": 99})[0]
    assert validate_metadata({**seeds["metadata"], "unknown": {}})[0]
