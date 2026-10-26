import { readFile } from "node:fs/promises";
import { join } from "node:path";
import {
  BUILD_PLATFORMS,
  DATA_ORIGINS,
  DATASET_NAMES,
  HACKATHON_MODES,
  HACKATHON_PLATFORMS,
  MODEL_MODALITIES,
  NEWS_SOURCES,
  SCHEMA_VERSION,
  SOURCE_IDS,
  SOURCE_STATUSES,
  type AIModelItem,
  type DataOrigin,
  type DatasetItemMap,
  type DatasetName,
  type HackathonItem,
  type MetadataSummary,
  type NewsItem,
  type SourceId,
  type SourceStatusEntry,
  type TechBuildItem,
} from "@/types/schema";
import { isSafeExternalUrl } from "@/lib/urls";

export type LoadStatus = "ok" | "missing" | "invalid";

export interface RejectedRecord {
  index: number;
  id: string | null;
  reasons: string[];
}

export interface DatasetResult<T> {
  status: LoadStatus;
  items: T[];
  dataOrigin: DataOrigin | null;
  generatedAtIso: string | null;
  rejected: RejectedRecord[];
  error: string | null;
}

export interface MetadataResult {
  status: LoadStatus;
  metadata: MetadataSummary | null;
  error: string | null;
}

interface RecordValidation<T> {
  item: T | null;
  id: string | null;
  reasons: string[];
}

type DatasetParserMap = {
  [K in DatasetName]: (raw: unknown) => RecordValidation<DatasetItemMap[K]>;
};

const ISO_DATE_TIME_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isIsoDateTime(value: unknown): value is string {
  if (typeof value !== "string") {
    return false;
  }

  const match = ISO_DATE_TIME_PATTERN.exec(value);
  if (match === null) {
    return false;
  }

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const isLeapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    31,
    isLeapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];

  return (
    year > 0 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth[month - 1]! &&
    Number.isFinite(Date.parse(value))
  );
}

function isNullableIsoDateTime(value: unknown): value is string | null {
  return value === null || isIsoDateTime(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
): value is T[number] {
  return typeof value === "string" && allowed.includes(value);
}

function idFromRecord(record: Record<string, unknown>): string | null {
  return isNonEmptyString(record.id) ? record.id : null;
}

function requiredString(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string {
  const value = record[field];
  if (isNonEmptyString(value)) {
    return value;
  }

  reasons.push(`${field} must be a non-empty string.`);
  return "";
}

function requiredDateTime(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string {
  const value = record[field];
  if (isIsoDateTime(value)) {
    return value;
  }

  reasons.push(`${field} must be a valid ISO date-time string.`);
  return "";
}

function requiredNumber(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): number {
  const value = record[field];
  if (isFiniteNumber(value)) {
    return value;
  }

  reasons.push(`${field} must be a finite number.`);
  return Number.NaN;
}

function requiredNullableNumber(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): number | null {
  const value = record[field];
  if (isNullableFiniteNumber(value)) {
    return value;
  }

  reasons.push(`${field} must be null or a finite number.`);
  return null;
}

function requiredStringArray(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string[] {
  const value = record[field];
  if (isStringArray(value)) {
    return value;
  }

  reasons.push(`${field} must be an array of strings.`);
  return [];
}

function requiredUrl(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string {
  const value = record[field];
  if (isSafeExternalUrl(value)) {
    return value;
  }

  reasons.push(`${field} must be a safe HTTPS URL.`);
  return "";
}

function optionalUrl(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string | undefined {
  if (!(field in record)) {
    return undefined;
  }

  const value = record[field];
  if (isSafeExternalUrl(value)) {
    return value;
  }

  // Unsafe optional URLs are dropped, but reported as record diagnostics without discarding the item.
  reasons.push(`${field} was dropped because it is not a safe HTTPS URL.`);
  return undefined;
}

function optionalNumber(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): number | undefined {
  if (!(field in record)) {
    return undefined;
  }

  const value = record[field];
  if (isFiniteNumber(value)) {
    return value;
  }

  reasons.push(`${field} must be a finite number when present.`);
  return undefined;
}

function optionalBoolean(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): boolean | undefined {
  if (!(field in record)) {
    return undefined;
  }

  const value = record[field];
  if (typeof value === "boolean") {
    return value;
  }

  reasons.push(`${field} must be a boolean when present.`);
  return undefined;
}

function optionalString(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): string | undefined {
  if (!(field in record)) {
    return undefined;
  }

  const value = record[field];
  if (isNonEmptyString(value)) {
    return value;
  }

  reasons.push(`${field} must be a non-empty string when present.`);
  return undefined;
}

function requiredEnum<const T extends readonly [string, ...string[]]>(
  record: Record<string, unknown>,
  field: string,
  allowed: T,
  reasons: string[],
): T[number] {
  const value = record[field];
  if (isOneOf(value, allowed)) {
    return value;
  }

  reasons.push(`${field} must be one of: ${allowed.join(", ")}.`);
  return allowed[0];
}

function requiredBoolean(
  record: Record<string, unknown>,
  field: string,
  reasons: string[],
): boolean {
  const value = record[field];
  if (typeof value === "boolean") {
    return value;
  }

  reasons.push(`${field} must be a boolean.`);
  return false;
}

function finishValidation<T>(
  item: T,
  id: string | null,
  reasons: string[],
  fatalReasonCount: number,
): RecordValidation<T> {
  return {
    item: fatalReasonCount === 0 ? item : null,
    id,
    reasons,
  };
}

function validateNewsItem(raw: unknown): RecordValidation<NewsItem> {
  if (!isObject(raw)) {
    return { item: null, id: null, reasons: ["record must be an object."] };
  }

  const reasons: string[] = [];
  const id = idFromRecord(raw);
  const title = requiredString(raw, "title", reasons);
  const summary = requiredString(raw, "summary", reasons);
  const url = requiredUrl(raw, "url", reasons);
  const source = requiredEnum(raw, "source", NEWS_SOURCES, reasons);
  const communityOrAuthor = requiredString(raw, "communityOrAuthor", reasons);
  const publishedAtIso = requiredDateTime(raw, "publishedAtIso", reasons);
  const upvotes = requiredNumber(raw, "upvotes", reasons);
  const commentsCount = requiredNumber(raw, "commentsCount", reasons);
  const tags = requiredStringArray(raw, "tags", reasons);

  if (id === null) {
    reasons.push("id must be a non-empty string.");
  }

  const fatalReasonCount = reasons.length;
  const thumbnailUrl = optionalUrl(raw, "thumbnailUrl", reasons);

  const item: NewsItem = {
    id: id ?? "",
    title,
    summary,
    url,
    source,
    communityOrAuthor,
    publishedAtIso,
    upvotes,
    commentsCount,
    tags,
    ...(thumbnailUrl === undefined ? {} : { thumbnailUrl }),
  };
  return finishValidation(item, id, reasons, fatalReasonCount);
}

function validateModelItem(raw: unknown): RecordValidation<AIModelItem> {
  if (!isObject(raw)) {
    return { item: null, id: null, reasons: ["record must be an object."] };
  }

  const reasons: string[] = [];
  const id = idFromRecord(raw);
  const name = requiredString(raw, "name", reasons);
  const provider = requiredString(raw, "provider", reasons);
  const arenaElo = requiredNullableNumber(raw, "arenaElo", reasons);
  const codingElo = requiredNullableNumber(raw, "codingElo", reasons);
  const contextLength = requiredNumber(raw, "contextLength", reasons);
  const inputCostPer1M = requiredNullableNumber(raw, "inputCostPer1M", reasons);
  const outputCostPer1M = requiredNullableNumber(raw, "outputCostPer1M", reasons);
  const modality = requiredEnum(raw, "modality", MODEL_MODALITIES, reasons);
  const isOpenWeights = requiredBoolean(raw, "isOpenWeights", reasons);
  const isFreeTierAvailable = requiredBoolean(raw, "isFreeTierAvailable", reasons);
  const categoryTags = requiredStringArray(raw, "categoryTags", reasons);
  const releaseDateIso = requiredDateTime(raw, "releaseDateIso", reasons);
  const openRouterUrl = requiredUrl(raw, "openRouterUrl", reasons);

  if (id === null) {
    reasons.push("id must be a non-empty string.");
  }

  const fatalReasonCount = reasons.length;
  const item: AIModelItem = {
    id: id ?? "",
    name,
    provider,
    arenaElo,
    codingElo,
    contextLength,
    inputCostPer1M,
    outputCostPer1M,
    modality,
    isOpenWeights,
    isFreeTierAvailable,
    categoryTags,
    releaseDateIso,
    openRouterUrl,
  };
  return finishValidation(item, id, reasons, fatalReasonCount);
}

function validateBuildItem(raw: unknown): RecordValidation<TechBuildItem> {
  if (!isObject(raw)) {
    return { item: null, id: null, reasons: ["record must be an object."] };
  }

  const reasons: string[] = [];
  const id = idFromRecord(raw);
  const title = requiredString(raw, "title", reasons);
  const tagline = requiredString(raw, "tagline", reasons);
  const sourcePlatform = requiredEnum(raw, "sourcePlatform", BUILD_PLATFORMS, reasons);
  const projectUrl = requiredUrl(raw, "projectUrl", reasons);
  const builders = requiredStringArray(raw, "builders", reasons);
  const techStack = requiredStringArray(raw, "techStack", reasons);
  const starsOrLikes = requiredNumber(raw, "starsOrLikes", reasons);
  const createdAtIso = requiredDateTime(raw, "createdAtIso", reasons);

  if (id === null) {
    reasons.push("id must be a non-empty string.");
  }

  const dailyVelocity = optionalNumber(raw, "dailyVelocity", reasons);
  const isWinner = optionalBoolean(raw, "isWinner", reasons);
  const hackathonName = optionalString(raw, "hackathonName", reasons);
  const nonUrlFatalReasonCount = reasons.length;
  const repoUrl = optionalUrl(raw, "repoUrl", reasons);
  const liveDemoUrl = optionalUrl(raw, "liveDemoUrl", reasons);
  const thumbnailUrl = optionalUrl(raw, "thumbnailUrl", reasons);

  const item: TechBuildItem = {
    id: id ?? "",
    title,
    tagline,
    sourcePlatform,
    projectUrl,
    ...(repoUrl === undefined ? {} : { repoUrl }),
    ...(liveDemoUrl === undefined ? {} : { liveDemoUrl }),
    ...(thumbnailUrl === undefined ? {} : { thumbnailUrl }),
    builders,
    techStack,
    starsOrLikes,
    ...(dailyVelocity === undefined ? {} : { dailyVelocity }),
    ...(isWinner === undefined ? {} : { isWinner }),
    ...(hackathonName === undefined ? {} : { hackathonName }),
    createdAtIso,
  };
  return finishValidation(item, id, reasons, nonUrlFatalReasonCount);
}

function validateHackathonItem(raw: unknown): RecordValidation<HackathonItem> {
  if (!isObject(raw)) {
    return { item: null, id: null, reasons: ["record must be an object."] };
  }

  const reasons: string[] = [];
  const id = idFromRecord(raw);
  const title = requiredString(raw, "title", reasons);
  const organizer = requiredString(raw, "organizer", reasons);
  const platform = requiredEnum(raw, "platform", HACKATHON_PLATFORMS, reasons);
  const mode = requiredEnum(raw, "mode", HACKATHON_MODES, reasons);
  const city = requiredString(raw, "city", reasons);
  const country = requiredString(raw, "country", reasons);
  const startDateIso = requiredDateTime(raw, "startDateIso", reasons);
  const deadlineIso = requiredDateTime(raw, "deadlineIso", reasons);
  const prizePoolText = requiredString(raw, "prizePoolText", reasons);
  const prizeValueNormalizedUsd = requiredNullableNumber(
    raw,
    "prizeValueNormalizedUsd",
    reasons,
  );
  const tags = requiredStringArray(raw, "tags", reasons);
  const registrationUrl = requiredUrl(raw, "registrationUrl", reasons);

  if (id === null) {
    reasons.push("id must be a non-empty string.");
  }

  const participantsCount = optionalNumber(raw, "participantsCount", reasons);
  const nonUrlFatalReasonCount = reasons.length;
  const bannerUrl = optionalUrl(raw, "bannerUrl", reasons);

  const item: HackathonItem = {
    id: id ?? "",
    title,
    organizer,
    platform,
    mode,
    city,
    country,
    startDateIso,
    deadlineIso,
    prizePoolText,
    prizeValueNormalizedUsd,
    ...(participantsCount === undefined ? {} : { participantsCount }),
    tags,
    ...(bannerUrl === undefined ? {} : { bannerUrl }),
    registrationUrl,
  };
  return finishValidation(item, id, reasons, nonUrlFatalReasonCount);
}

const datasetParsers: DatasetParserMap = {
  news: validateNewsItem,
  models: validateModelItem,
  builds: validateBuildItem,
  hackathons: validateHackathonItem,
};

function invalidDatasetResult<T>(error: string): DatasetResult<T> {
  return {
    status: "invalid",
    items: [],
    dataOrigin: null,
    generatedAtIso: null,
    rejected: [],
    error,
  };
}

function invalidMetadataResult(error: string): MetadataResult {
  return { status: "invalid", metadata: null, error };
}

function parseDatasetInternal<K extends DatasetName>(
  kind: K,
  raw: unknown,
): DatasetResult<DatasetItemMap[K]> {
  if (
    !isObject(raw) ||
    raw.schemaVersion !== SCHEMA_VERSION ||
    !isOneOf(raw.dataOrigin, DATA_ORIGINS) ||
    !isIsoDateTime(raw.generatedAtIso) ||
    !Array.isArray(raw.items)
  ) {
    return invalidDatasetResult("Dataset envelope is invalid.");
  }

  const items: DatasetItemMap[K][] = [];
  const rejected: RejectedRecord[] = [];
  const seenIds = new Set<string>();

  raw.items.forEach((entry, index) => {
    const result = datasetParsers[kind](entry);
    const id = result.id;

    if (result.item !== null && id !== null && seenIds.has(id)) {
      result.item = null;
      result.reasons.push("duplicate id; first occurrence was kept.");
    }

    if (result.item !== null && id !== null) {
      seenIds.add(id);
      items.push(result.item);
    }

    if (result.reasons.length > 0) {
      rejected.push({ index, id, reasons: result.reasons });
    }
  });

  return {
    status: "ok",
    items,
    dataOrigin: raw.dataOrigin,
    generatedAtIso: raw.generatedAtIso,
    rejected,
    error: null,
  };
}

export function parseDataset<K extends DatasetName>(
  kind: K,
  raw: unknown,
): DatasetResult<DatasetItemMap[K]> {
  try {
    if (!isOneOf(kind, DATASET_NAMES)) {
      return invalidDatasetResult("Dataset kind is invalid.");
    }
    return parseDatasetInternal(kind, raw);
  } catch {
    return invalidDatasetResult("Dataset could not be validated.");
  }
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableFiniteInteger(value: unknown): value is number | null {
  return value === null || (isFiniteNumber(value) && Number.isInteger(value));
}

function parseMetadataInternal(raw: unknown): MetadataResult {
  if (
    !isObject(raw) ||
    raw.schemaVersion !== SCHEMA_VERSION ||
    !isOneOf(raw.dataOrigin, DATA_ORIGINS) ||
    !isIsoDateTime(raw.lastUpdatedIso) ||
    !isNullableIsoDateTime(raw.lastAttemptIso) ||
    !isObject(raw.counts) ||
    !isObject(raw.sources)
  ) {
    return invalidMetadataResult("Metadata is invalid.");
  }

  const counts: Record<DatasetName, number> = {
    news: 0,
    models: 0,
    builds: 0,
    hackathons: 0,
  };

  for (const name of DATASET_NAMES) {
    const count = raw.counts[name];
    if (!isFiniteNumber(count) || !Number.isInteger(count) || count < 0) {
      return invalidMetadataResult("Metadata counts are invalid.");
    }
    counts[name] = count;
  }

  const sources: Partial<Record<SourceId, SourceStatusEntry>> = {};
  for (const [sourceId, sourceValue] of Object.entries(raw.sources)) {
    if (
      !isOneOf(sourceId, SOURCE_IDS) ||
      !isObject(sourceValue) ||
      !isOneOf(sourceValue.status, SOURCE_STATUSES) ||
      !isNullableIsoDateTime(sourceValue.lastSuccessIso) ||
      !isNullableIsoDateTime(sourceValue.lastAttemptIso) ||
      !isNullableFiniteInteger(sourceValue.recordsFetched) ||
      !isNullableFiniteInteger(sourceValue.recordsAccepted) ||
      !isNullableString(sourceValue.errorSummary)
    ) {
      return invalidMetadataResult("Metadata source status is invalid.");
    }

    sources[sourceId] = {
      status: sourceValue.status,
      lastSuccessIso: sourceValue.lastSuccessIso,
      lastAttemptIso: sourceValue.lastAttemptIso,
      recordsFetched: sourceValue.recordsFetched,
      recordsAccepted: sourceValue.recordsAccepted,
      errorSummary: sourceValue.errorSummary,
    };
  }

  const metadata: MetadataSummary = {
    schemaVersion: SCHEMA_VERSION,
    dataOrigin: raw.dataOrigin,
    lastUpdatedIso: raw.lastUpdatedIso,
    lastAttemptIso: raw.lastAttemptIso,
    counts,
    sources,
  };
  return { status: "ok", metadata, error: null };
}

export function parseMetadata(raw: unknown): MetadataResult {
  try {
    return parseMetadataInternal(raw);
  } catch {
    return invalidMetadataResult("Metadata could not be validated.");
  }
}

function hasErrorCode(error: unknown, code: string): boolean {
  return isObject(error) && error.code === code;
}

function missingDatasetResult<T>(): DatasetResult<T> {
  return {
    status: "missing",
    items: [],
    dataOrigin: null,
    generatedAtIso: null,
    rejected: [],
    error: "Dataset file is missing.",
  };
}

function unreadableDatasetResult<T>(): DatasetResult<T> {
  return invalidDatasetResult("Dataset file could not be read.");
}

function missingMetadataResult(): MetadataResult {
  return { status: "missing", metadata: null, error: "Metadata file is missing." };
}

export async function loadDataset<K extends DatasetName>(
  kind: K,
): Promise<DatasetResult<DatasetItemMap[K]>> {
  try {
    if (!isOneOf(kind, DATASET_NAMES)) {
      return invalidDatasetResult("Dataset kind is invalid.");
    }

    const filePath = join(process.cwd(), "public", "data", `${kind}.json`);
    let contents: string;

    try {
      contents = await readFile(filePath, "utf8");
    } catch (error) {
      return hasErrorCode(error, "ENOENT")
        ? missingDatasetResult()
        : unreadableDatasetResult();
    }

    let raw: unknown;
    try {
      raw = JSON.parse(contents);
    } catch {
      return invalidDatasetResult("Dataset file contains invalid JSON.");
    }

    return parseDataset(kind, raw);
  } catch {
    return unreadableDatasetResult();
  }
}

export async function loadMetadata(): Promise<MetadataResult> {
  try {
    const filePath = join(process.cwd(), "public", "data", "metadata.json");
    let contents: string;

    try {
      contents = await readFile(filePath, "utf8");
    } catch (error) {
      return hasErrorCode(error, "ENOENT")
        ? missingMetadataResult()
        : invalidMetadataResult("Metadata file could not be read.");
    }

    let raw: unknown;
    try {
      raw = JSON.parse(contents);
    } catch {
      return invalidMetadataResult("Metadata file contains invalid JSON.");
    }

    return parseMetadata(raw);
  } catch {
    return invalidMetadataResult("Metadata file could not be read.");
  }
}

export async function loadAll(): Promise<{
  metadata: MetadataResult;
  news: DatasetResult<NewsItem>;
  models: DatasetResult<AIModelItem>;
  builds: DatasetResult<TechBuildItem>;
  hackathons: DatasetResult<HackathonItem>;
}> {
  // The Python publisher (Phase 2) must emit the same canonical envelope.
  const [metadata, news, models, builds, hackathons] = await Promise.all([
    loadMetadata(),
    loadDataset("news"),
    loadDataset("models"),
    loadDataset("builds"),
    loadDataset("hackathons"),
  ]);

  return { metadata, news, models, builds, hackathons };
}
