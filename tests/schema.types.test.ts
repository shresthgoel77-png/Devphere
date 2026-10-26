import { describe, expect, it } from "vitest";
import {
  BUILD_PLATFORMS,
  DATASET_NAMES,
  DATA_ORIGINS,
  HACKATHON_MODES,
  HACKATHON_PLATFORMS,
  MODEL_MODALITIES,
  NEWS_SOURCES,
  SCHEMA_VERSION,
  SOURCE_IDS,
  SOURCE_STATUSES,
  type AIModelItem,
  type DatasetEnvelope,
  type DatasetItemMap,
  type HackathonItem,
  type MetadataSummary,
  type NewsItem,
  type SourceStatusEntry,
  type TechBuildItem,
} from "@/types/schema";

const news: NewsItem = {
  id: "fake-news-1",
  title: "Fake news item",
  summary: "A clearly fictional summary for schema testing.",
  url: "https://example.invalid/news",
  source: "HackerNews",
  communityOrAuthor: "Fake author",
  publishedAtIso: "2026-01-01T00:00:00.000Z",
  upvotes: 0,
  commentsCount: 0,
  tags: ["fictional"],
};

const modelWithUnknownCost: AIModelItem = {
  id: "fake-model-unknown-cost",
  name: "Fictional Model",
  provider: "Example Provider",
  arenaElo: null,
  codingElo: null,
  contextLength: 0,
  inputCostPer1M: null,
  outputCostPer1M: null,
  modality: "Text",
  isOpenWeights: false,
  isFreeTierAvailable: false,
  categoryTags: ["fictional"],
  releaseDateIso: "2026-01-01",
  openRouterUrl: "https://example.invalid/models/fake",
};

const modelWithZeroCost: AIModelItem = {
  ...modelWithUnknownCost,
  id: "fake-model-zero-cost",
  inputCostPer1M: 0,
};

const build: TechBuildItem = {
  id: "fake-build-1",
  title: "Fictional Build",
  tagline: "A clearly fictional project.",
  sourcePlatform: "GitHub",
  projectUrl: "https://example.invalid/build",
  builders: ["Fake builder"],
  techStack: ["TypeScript"],
  starsOrLikes: 0,
  createdAtIso: "2026-01-01T00:00:00.000Z",
};

const hackathon: HackathonItem = {
  id: "fake-hackathon-1",
  title: "Fictional Hackathon",
  organizer: "Example Organizer",
  platform: "Devpost",
  mode: "Online",
  city: "Example City",
  country: "Example Country",
  startDateIso: "2026-02-01T00:00:00.000Z",
  deadlineIso: "2026-01-31T23:59:59.000Z",
  prizePoolText: "Unknown",
  prizeValueNormalizedUsd: null,
  tags: ["fictional"],
  registrationUrl: "https://example.invalid/register",
};

const envelope: DatasetEnvelope<NewsItem> = {
  schemaVersion: SCHEMA_VERSION,
  dataOrigin: "seed",
  generatedAtIso: "2026-01-01T00:00:00.000Z",
  items: [news],
};

const sourceStatus: SourceStatusEntry = {
  status: "no_data",
  lastSuccessIso: null,
  lastAttemptIso: null,
  recordsFetched: null,
  recordsAccepted: null,
  errorSummary: null,
};

const metadata: MetadataSummary = {
  schemaVersion: SCHEMA_VERSION,
  dataOrigin: "seed",
  lastUpdatedIso: "2026-01-01T00:00:00.000Z",
  lastAttemptIso: null,
  counts: {
    news: 1,
    models: 2,
    builds: 1,
    hackathons: 1,
  },
  sources: { hackernews: sourceStatus },
};

const datasetItems: DatasetItemMap = {
  news,
  models: modelWithUnknownCost,
  builds: build,
  hackathons: hackathon,
};

// @ts-expect-error Invalid values are rejected by the canonical source union.
const invalidNewsSource: NewsItem["source"] = "NotARealSource";
void invalidNewsSource;

describe("canonical data schema", () => {
  it("exports the exact allowed values for each union", () => {
    expect(SCHEMA_VERSION).toBe(1);
    expect(DATA_ORIGINS).toEqual(["seed", "ingested"]);
    expect(DATASET_NAMES).toEqual(["news", "models", "builds", "hackathons"]);
    expect(NEWS_SOURCES).toEqual([
      "HackerNews",
      "Reddit",
      "Dev.to",
      "HuggingFace",
      "YouTube",
    ]);
    expect(MODEL_MODALITIES).toEqual(["Text", "Multimodal", "Vision", "Audio"]);
    expect(BUILD_PLATFORMS).toEqual([
      "GitHub",
      "Devfolio",
      "Devpost",
      "ShowHN",
    ]);
    expect(HACKATHON_PLATFORMS).toEqual([
      "Unstop",
      "Devfolio",
      "Devpost",
      "Commudle",
    ]);
    expect(HACKATHON_MODES).toEqual(["Online", "In-Person", "Hybrid"]);
    expect(SOURCE_IDS).toEqual([
      "hackernews",
      "reddit",
      "devto",
      "huggingface",
      "youtube",
      "openrouter",
      "arena",
      "github",
      "devfolio",
      "devpost",
      "unstop",
      "commudle",
    ]);
    expect(SOURCE_STATUSES).toEqual([
      "success",
      "partial",
      "failed",
      "rate_limited",
      "no_data",
      "stale",
    ]);
  });

  it("accepts typed examples and distinguishes unknown from zero cost", () => {
    expect(modelWithUnknownCost.inputCostPer1M).toBeNull();
    expect(modelWithZeroCost.inputCostPer1M).toBe(0);
    expect(envelope.items).toEqual([news]);
    expect(datasetItems).toMatchObject({ builds: build, hackathons: hackathon });
    expect(metadata.counts.models).toBe(2);
  });
});
