# Phase 2 data ingestion

The Python ingestion pipeline fetches selected public sources, validates their
records against the shared schema, merges verified records with last-known-good
data, and atomically publishes the `news.json`, `models.json`, and
`metadata.json` files. It does not ingest or write `builds.json` or
`hackathons.json`.

The pipeline uses unauthenticated public endpoints and needs no API keys. Do not
assume an endpoint will remain available or free: check its current terms,
rate limits, and operating assumptions before deployment. Failed or blocked
requests are reported; the pipeline does not attempt to evade them.

## Setup and tests

Use Python 3.11. From the repository root:

```sh
cd scripts
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

In Windows PowerShell, create the environment with `py -3.11 -m venv .venv`,
activate it with `.\.venv\Scripts\Activate.ps1`, and use the same `python -m`
commands. The requirements are bounded compatible-version ranges rather than a
fully locked environment. The fetcher tests use mocked responses and fixtures;
they do not require live endpoints.

## Running the orchestrator

From `scripts/`, inspect the current CLI with:

```sh
python ingest_all.py --help
```

The default `--data-dir` is the repository's `public/data/`. A normal run
performs live requests and may publish validated `news.json`, `models.json`,
and `metadata.json` there. Review that behavior carefully before running it.
Use a temporary data directory to keep a live trial away from published data:

```sh
python ingest_all.py --data-dir ../tmp-ingestion-data
```

`--dry-run` still makes live source requests, validates and summarizes the
candidates, but does not publish datasets or metadata. A dry-run
`--summary-json` path must be outside `--data-dir`.

| Flag | Behavior |
| --- | --- |
| `--data-dir PATH` | Read and publish data in this directory; defaults to `../public/data/` from `scripts/`. |
| `--sources ID,ID,...` | Select source IDs; defaults to all sources currently registered in this checkout. IDs must be unique and registered. |
| `--dry-run` | Fetch and validate without publishing datasets or metadata. |
| `--summary-json PATH` | Also write the run summary to this file; it cannot overwrite a published file. |
| `--strict` | Return exit code 3 when any selected source has an error status. |
| `--verbose` | Enable debug-level logging. |

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | No publication refusal; in non-strict mode, source errors can still be present. |
| `1` | Dataset or metadata publication was refused, corruption could not be recovered, or an unexpected run error occurred. |
| `2` | CLI/configuration usage error, including an unknown source or invalid data directory. |
| `3` | `--strict` was used and a source reported an error status. |

Argument parsing errors also use argparse's exit code `2`.

## Architecture and data flow

1. The CLI resolves the selected source IDs and validates the run configuration.
2. The orchestrator loads and validates the current envelopes and metadata.
3. Selected asynchronous fetchers call public endpoints through a shared bounded
   HTTP client. The fetch layer limits time, response size, redirects, and
   retries; it retries only eligible transient failures and does not retry
   blocks. Each source normalizes its response into schema-shaped records.
4. The orchestrator validates records and records per-source counts, statuses,
   and safe error summaries. OpenRouter model records can be passed through
   conservative benchmark enrichment if a benchmark source is present.
5. For `news` and `models`, accepted new records are merged by ID with
   last-known-good non-seed records, sorted, and pruned where applicable.
   Candidates are validated and checked against item and byte limits before
   publication.
6. `storage.py` writes validated files by a temporary file and atomic replace.
   A failed or empty fetch does not replace a valid non-empty dataset with an
   empty one. `builds.json` and `hackathons.json` are left untouched.
7. The orchestrator builds metadata from the actual files on disk and prints a
   JSON run summary. A summary file is optional.

### Registered sources

The status values used in source metadata are `success`, `partial`, `failed`,
`rate_limited`, `no_data`, and `stale`. `stale` is assigned by the orchestrator
when a failed or rate-limited source has a prior successful record older than
the 18-hour stale threshold. Requests do not use credentials or API keys.

| Source ID | Public endpoint(s) | Auth | Source statuses | Notes and limitations |
| --- | --- | --- | --- | --- |
| `hackernews` | `https://hacker-news.firebaseio.com/v0/{topstories,beststories,item/<id>}.json` | None | `success`, `no_data`, `partial`, `failed`, `rate_limited` | Reads at most 60 top and 40 best IDs, deduplicates them, and caps item requests at 100. Some records may be skipped when required fields are missing. |
| `reddit` | `https://www.reddit.com/r/<subreddit>/hot.json?limit=35`; fallback `https://www.reddit.com/r/<subreddit>/hot/.rss` | None | `success`, `no_data`, `partial`, `failed`, `rate_limited` | Checks eight configured subreddits sequentially, with one RSS fallback after a JSON failure and a polite delay. Reddit is commonly blocked from datacenter IPs; a block is reported, not bypassed. RSS does not supply reliable metrics, so its counts are null. |
| `devto` | `https://dev.to/api/articles?tag=<tag>&top=2&per_page=30` | None | `success`, `no_data`, `partial`, `failed`, `rate_limited` | Fetches seven configured tags with bounded concurrency, then deduplicates articles. Coverage is limited to those configured tags and the endpoint response. |
| `huggingface` | `https://huggingface.co/api/daily_papers` | None | `success`, `no_data`, `failed` | Includes daily papers with usable IDs and dates. The upstream GitHub repository URL is intentionally omitted because the news schema has no field for it. |
| `youtube` | `https://www.youtube.com/feeds/videos.xml?channel_id=<channel_id>` | None | `success`, `no_data`, `partial`, `failed` | Reads at most ten entries from each of six configured channel feeds. Feed totals are not reliable likes/comments and are not represented: both metrics are null. A failed channel can make the source partial. |
| `openrouter` | `https://openrouter.ai/api/v1/models` | None | `success`, `no_data`, `failed`, `rate_limited` | Excludes records that explicitly declare no text output; an absent output-modality declaration is treated as eligible. Missing prices remain null. “Free tier available” is set only when both listed prices are zero; it is not a guarantee of continued availability. Arena and coding benchmark values are unknown unless a future benchmark source supplies verified rows. |

When a failed or rate-limited source has a prior successful record older than
18 hours, the orchestrator can record `stale` instead of that error status.
This is a metadata state derived from prior success, not a fetcher result.

## Schema and provenance

Each dataset envelope uses schema version `1`, a dataset-specific
`dataOrigin` (`seed` or `ingested`), a `generatedAtIso` timestamp, and `items`.
`news.summary` is a required nullable field: `null` means no summary is known.
Nullable numeric fields likewise use `null` for unknown; zero is reserved for a
verified zero. Optional fields may be omitted where the schema permits. The
TypeScript schema in `src/types/schema.ts` is canonical and is mirrored by
`scripts/models.py`.

`metadata.json` has its own `dataOrigin`, run timestamps, dataset counts, and
per-source status information. Its `dataOrigin` is `ingested` only when all
four on-disk datasets (`news`, `models`, `builds`, and `hackathons`) are
ingested. It can therefore remain `seed` even after `news` and `models` have
been ingested, because the two other datasets are not ingested by this phase.
Per-dataset `dataOrigin` describes that dataset alone; source statuses describe
fetch attempts and are not themselves a data-origin indicator.

Metadata may include a `benchmarks` provenance object containing a source name,
a safe HTTPS source URL, a nullable snapshot timestamp, and a fetch timestamp.
The benchmark merger matches only unique normalized model names. In this
checkout there is no `scripts/sources/arena.py` fetcher, so the merger and
provenance validation are scaffolding, not a live benchmark collection
capability.

## Retention and last-known-good data

- News is limited to the most recent 90 days and at most 500 records.
- Models are limited to 1,000 records; there is no age-based model retention.
- Dataset files are limited to 2,000,000 bytes by the orchestrator.
- No archive exists yet. Records removed by age or size limits are discarded
  from the published dataset, not archived elsewhere.

When a run has no accepted replacement records, a valid existing non-empty
dataset is left in place. Successful records merge with prior ingested records;
seed records are excluded during the seed-to-ingested transition. Publication
validates the candidate and uses an atomic replace. This protects the last
valid on-disk file from ordinary failed/empty runs, but it is not a substitute
for a versioned backup. The tests exercise failure preservation using temporary
directories; no live run has been verified as part of this documentation.

### Manual recovery from Git history

First identify a commit containing the known-good version and save both a
recovery copy and the current file before restoring. In a POSIX-style shell:

```sh
git show <commit>:public/data/news.json > /tmp/news.json.last-good
cp public/data/news.json /tmp/news.json.before-restore
git diff --no-index public/data/news.json /tmp/news.json.last-good
# After confirming the candidate and backup, restore explicitly:
cp /tmp/news.json.last-good public/data/news.json
```

Repeat for the affected dataset or metadata file. Do not replace a file until
the selected historical content and backup have been checked. The orchestrator
does not search Git history or perform this manual recovery automatically.

## Not implemented and open decisions

The current source registry has no ingestors for GitHub, Devfolio, Devpost,
Unstop, Commudle, or Show HN projects. Show HN posts can be tagged in news, but
there is no separate project ingestion. Playwright-based collection, data
archives, and a GitHub Actions ingestion workflow are also not implemented.
The schema's platform/source identifiers do not imply that these integrations
exist.

Before expanding collection, decide whether to add Frontier Reasoning and
Coding Specialist tags, whether to collect Hard Prompts Elo and/or MMLU-Pro
benchmarks, how Show HN projects should be sourced, and how archives should
work. Benchmark scores and provenance must remain unknown unless supported by
verified source data.

## Deployment and cost assumptions

This implementation has no account/authentication layer, backend server, or
database, and its configured public sources do not require API credentials.
Those facts do not establish that hosting, source access, or future operation
will be free indefinitely. Verify hosting terms, source terms, rate limits,
availability, and any no-card/$0 assumptions at deployment time. No deployment
or “free forever” guarantee is made here.
