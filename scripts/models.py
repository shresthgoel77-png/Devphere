# src/types/schema.ts is canonical. Coordinate changes across Python, TypeScript,
# fixtures, UI, and tests; do not independently redesign the ingestion contract.
"""Contract data and result types. No third-party imports or source fetching."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Literal, Mapping

SCHEMA_VERSION: Final = 1
DATA_ORIGINS: Final = ("seed", "ingested")
DATASET_NAMES: Final = ("news", "models", "builds", "hackathons")
NEWS_SOURCES: Final = ("HackerNews", "Reddit", "Dev.to", "HuggingFace", "YouTube")
MODEL_MODALITIES: Final = ("Text", "Multimodal", "Vision", "Audio")
BUILD_PLATFORMS: Final = ("GitHub", "Devfolio", "Devpost", "ShowHN")
HACKATHON_PLATFORMS: Final = ("Unstop", "Devfolio", "Devpost", "Commudle")
HACKATHON_MODES: Final = ("Online", "In-Person", "Hybrid")
SOURCE_IDS: Final = (
    "hackernews", "reddit", "devto", "huggingface", "youtube", "openrouter",
    "arena", "github", "devfolio", "devpost", "unstop", "commudle",
)
SOURCE_STATUSES: Final = ("success", "partial", "failed", "rate_limited", "no_data", "stale")

FieldKind = Literal["string", "datetime", "number", "nullable_number", "boolean", "strings", "enum", "url"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    required: bool = True
    choices: tuple[str, ...] = ()


# Ordered exactly as the canonical TS interfaces/output records. Nullable numbers
# are required fields: absence is invalid, None is unknown, and 0 is verified zero.
DATASET_FIELDS: Final[Mapping[str, tuple[FieldSpec, ...]]] = {
    "news": (
        FieldSpec("id", "string"), FieldSpec("title", "string"),
        FieldSpec("summary", "string"), FieldSpec("url", "url"),
        FieldSpec("source", "enum", choices=NEWS_SOURCES),
        FieldSpec("communityOrAuthor", "string"), FieldSpec("publishedAtIso", "datetime"),
        FieldSpec("upvotes", "number"), FieldSpec("commentsCount", "number"),
        FieldSpec("tags", "strings"), FieldSpec("thumbnailUrl", "url", False),
    ),
    "models": (
        FieldSpec("id", "string"), FieldSpec("name", "string"), FieldSpec("provider", "string"),
        FieldSpec("arenaElo", "nullable_number"), FieldSpec("codingElo", "nullable_number"),
        FieldSpec("contextLength", "number"), FieldSpec("inputCostPer1M", "nullable_number"),
        FieldSpec("outputCostPer1M", "nullable_number"),
        FieldSpec("modality", "enum", choices=MODEL_MODALITIES),
        FieldSpec("isOpenWeights", "boolean"), FieldSpec("isFreeTierAvailable", "boolean"),
        FieldSpec("categoryTags", "strings"), FieldSpec("releaseDateIso", "datetime"),
        FieldSpec("openRouterUrl", "url"),
    ),
    "builds": (
        FieldSpec("id", "string"), FieldSpec("title", "string"), FieldSpec("tagline", "string"),
        FieldSpec("sourcePlatform", "enum", choices=BUILD_PLATFORMS),
        FieldSpec("projectUrl", "url"), FieldSpec("repoUrl", "url", False),
        FieldSpec("liveDemoUrl", "url", False), FieldSpec("thumbnailUrl", "url", False),
        FieldSpec("builders", "strings"), FieldSpec("techStack", "strings"),
        FieldSpec("starsOrLikes", "number"), FieldSpec("dailyVelocity", "number", False),
        FieldSpec("isWinner", "boolean", False), FieldSpec("hackathonName", "string", False),
        FieldSpec("createdAtIso", "datetime"),
    ),
    "hackathons": (
        FieldSpec("id", "string"), FieldSpec("title", "string"), FieldSpec("organizer", "string"),
        FieldSpec("platform", "enum", choices=HACKATHON_PLATFORMS),
        FieldSpec("mode", "enum", choices=HACKATHON_MODES),
        FieldSpec("city", "string"), FieldSpec("country", "string"),
        FieldSpec("startDateIso", "datetime"), FieldSpec("deadlineIso", "datetime"),
        FieldSpec("prizePoolText", "string"), FieldSpec("prizeValueNormalizedUsd", "nullable_number"),
        FieldSpec("participantsCount", "number", False), FieldSpec("tags", "strings"),
        FieldSpec("bannerUrl", "url", False), FieldSpec("registrationUrl", "url"),
    ),
}


def sanitize_error(error: object) -> str:
    """Never echo untrusted exception text (it can contain credentials or paths).

    Keep a useful, allowlisted category; arbitrary upstream strings are withheld.
    This is deliberately conservative rather than trying to recognize every secret.
    """
    category = "operation"
    for kind, label in ((TimeoutError, "timeout"), (OSError, "I/O"),
                        (ValueError, "value"), (TypeError, "type")):
        if isinstance(error, kind):
            category = label
            break
    return f"{category} failure; untrusted error details withheld."[:300]


@dataclass
class SourceResult:
    source_id: str
    status: str
    records: list[dict] = field(default_factory=list)
    fetched: int = 0
    error_summary: str | None = None
    started_iso: str | None = None
    finished_iso: str | None = None
    latency_ms: float | None = None

    @classmethod
    def failure(cls, source_id: str, error: object, *, started_iso: str | None = None,
                finished_iso: str | None = None, latency_ms: float | None = None) -> "SourceResult":
        now = datetime.now(timezone.utc).isoformat()
        return cls(source_id, "failed", error_summary=sanitize_error(error),
                   started_iso=started_iso or now, finished_iso=finished_iso or now,
                   latency_ms=latency_ms)


@dataclass
class RejectedRecord:
    index: int
    id: str | None
    reasons: list[str]


@dataclass
class ValidationReport:
    accepted: list[dict] = field(default_factory=list)
    # Mirrors TS: includes optional-URL diagnostics even for accepted records.
    rejected: list[RejectedRecord] = field(default_factory=list)
    dropped_optional: list[RejectedRecord] = field(default_factory=list)


@dataclass
class PublishOutcome:
    dataset: str
    published: bool
    reason: str
    path: str
    bytes: int = 0
    item_count: int = 0
    unchanged: bool = False
