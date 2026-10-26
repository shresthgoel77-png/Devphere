// The Python models in scripts/models.py (Phase 2) must mirror this file; coordinate schema changes across Python, TypeScript, fixtures, UI, and tests.

export const SCHEMA_VERSION = 1 as const;

export const DATA_ORIGINS = ["seed", "ingested"] as const;
export type DataOrigin = "seed" | "ingested";

export const DATASET_NAMES = ["news", "models", "builds", "hackathons"] as const;
export type DatasetName = "news" | "models" | "builds" | "hackathons";

export interface DatasetEnvelope<T> {
  schemaVersion: number;
  dataOrigin: DataOrigin;
  generatedAtIso: string;
  items: T[];
}

export const NEWS_SOURCES = [
  "HackerNews",
  "Reddit",
  "Dev.to",
  "HuggingFace",
  "YouTube",
] as const;

export interface NewsItem {
  id: string;
  title: string;
  summary: string;
  url: string;
  source: "HackerNews" | "Reddit" | "Dev.to" | "HuggingFace" | "YouTube";
  communityOrAuthor: string;
  publishedAtIso: string;
  upvotes: number;
  commentsCount: number;
  tags: string[];
  thumbnailUrl?: string;
}

export const MODEL_MODALITIES = [
  "Text",
  "Multimodal",
  "Vision",
  "Audio",
] as const;

export interface AIModelItem {
  id: string;
  name: string;
  provider: string;
  /** Null means the arena ELO is unknown, not zero. */
  arenaElo: number | null;
  /** Null means the coding ELO is unknown, not zero. */
  codingElo: number | null;
  contextLength: number;
  /** Null means the input cost is unknown, not zero. */
  inputCostPer1M: number | null;
  /** Null means the output cost is unknown, not zero. */
  outputCostPer1M: number | null;
  modality: "Text" | "Multimodal" | "Vision" | "Audio";
  isOpenWeights: boolean;
  isFreeTierAvailable: boolean;
  categoryTags: string[];
  releaseDateIso: string;
  openRouterUrl: string;
}

export const BUILD_PLATFORMS = [
  "GitHub",
  "Devfolio",
  "Devpost",
  "ShowHN",
] as const;

export interface TechBuildItem {
  id: string;
  title: string;
  tagline: string;
  sourcePlatform: "GitHub" | "Devfolio" | "Devpost" | "ShowHN";
  projectUrl: string;
  repoUrl?: string;
  liveDemoUrl?: string;
  thumbnailUrl?: string;
  builders: string[];
  techStack: string[];
  starsOrLikes: number;
  dailyVelocity?: number;
  isWinner?: boolean;
  hackathonName?: string;
  createdAtIso: string;
}

export const HACKATHON_PLATFORMS = [
  "Unstop",
  "Devfolio",
  "Devpost",
  "Commudle",
] as const;

export const HACKATHON_MODES = ["Online", "In-Person", "Hybrid"] as const;

export interface HackathonItem {
  id: string;
  title: string;
  organizer: string;
  platform: "Unstop" | "Devfolio" | "Devpost" | "Commudle";
  mode: "Online" | "In-Person" | "Hybrid";
  city: string;
  country: string;
  startDateIso: string;
  deadlineIso: string;
  prizePoolText: string;
  /** Null means the normalized prize value is unknown, not zero. */
  prizeValueNormalizedUsd: number | null;
  participantsCount?: number;
  tags: string[];
  bannerUrl?: string;
  registrationUrl: string;
}

export const SOURCE_IDS = [
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
] as const;
export type SourceId =
  | "hackernews"
  | "reddit"
  | "devto"
  | "huggingface"
  | "youtube"
  | "openrouter"
  | "arena"
  | "github"
  | "devfolio"
  | "devpost"
  | "unstop"
  | "commudle";

export const SOURCE_STATUSES = [
  "success",
  "partial",
  "failed",
  "rate_limited",
  "no_data",
  "stale",
] as const;
export type SourceStatus =
  | "success"
  | "partial"
  | "failed"
  | "rate_limited"
  | "no_data"
  | "stale";

export interface SourceStatusEntry {
  status: SourceStatus;
  lastSuccessIso: string | null;
  lastAttemptIso: string | null;
  recordsFetched: number | null;
  recordsAccepted: number | null;
  errorSummary: string | null;
}

export interface MetadataSummary {
  schemaVersion: number;
  dataOrigin: DataOrigin;
  lastUpdatedIso: string;
  lastAttemptIso: string | null;
  counts: {
    news: number;
    models: number;
    builds: number;
    hackathons: number;
  };
  sources: Partial<Record<SourceId, SourceStatusEntry>>;
}

export type DatasetItemMap = {
  news: NewsItem;
  models: AIModelItem;
  builds: TechBuildItem;
  hackathons: HackathonItem;
};
