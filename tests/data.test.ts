import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import newsSeed from "../public/data/news.json";
import modelsSeed from "../public/data/models.json";
import buildsSeed from "../public/data/builds.json";
import hackathonsSeed from "../public/data/hackathons.json";
import metadataSeed from "../public/data/metadata.json";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  loadAll,
  loadDataset,
  loadMetadata,
  parseDataset,
  parseMetadata,
} from "@/lib/data";
import type { DatasetName } from "@/types/schema";

const seeds = {
  news: newsSeed,
  models: modelsSeed,
  builds: buildsSeed,
  hackathons: hackathonsSeed,
};

const seedCounts = metadataSeed.counts;
let temporaryDirectory: string | null = null;
let cwdSpy: ReturnType<typeof vi.spyOn> | null = null;

async function useTemporaryDataDirectory(): Promise<string> {
  temporaryDirectory = await mkdtemp(join(tmpdir(), "devnexus-data-"));
  await mkdir(join(temporaryDirectory, "public", "data"), { recursive: true });
  cwdSpy = vi.spyOn(process, "cwd").mockReturnValue(temporaryDirectory);
  return join(temporaryDirectory, "public", "data");
}

afterEach(async () => {
  cwdSpy?.mockRestore();
  cwdSpy = null;
  if (temporaryDirectory !== null) {
    await rm(temporaryDirectory, { recursive: true, force: true });
    temporaryDirectory = null;
  }
});

describe("parseDataset", () => {
  it.each(["news", "models", "builds", "hackathons"] as const)(
    "accepts the %s seed file with no rejected records",
    (kind: DatasetName) => {
      const result = parseDataset(kind, seeds[kind]);
      expect(result.status).toBe("ok");
      expect(result.rejected).toEqual([]);
      expect(result.items).toHaveLength(seedCounts[kind]);
    },
  );

  it("rejects invalid envelopes without throwing", () => {
    const seed = seeds.news;
    expect(
      parseDataset("news", { ...seed, schemaVersion: 99 }).status,
    ).toBe("invalid");
    expect(parseDataset("news", { ...seed, items: {} }).status).toBe("invalid");
    expect(parseDataset("news", null).status).toBe("invalid");
    expect(parseDataset("news", "garbage").status).toBe("invalid");
  });

  it("rejects malformed records with specific reasons and preserves valid siblings", () => {
    const original = newsSeed.items[0]!;
    const validWithUnsafeThumbnail = {
      ...original,
      thumbnailUrl: "javascript:alert(1)",
    };
    const invalidCases = [
      { ...original, title: undefined },
      { ...original, source: "NotARealSource" },
      { ...original, upvotes: Number.NaN },
      { ...original, url: "javascript:alert(1)" },
      { ...original, publishedAtIso: "not-a-date" },
      { ...original, publishedAtIso: "2026-02-30T12:00:00Z" },
      { ...original, tags: ["valid", 42] },
      { ...original, id: original.id },
    ];
    const result = parseDataset("news", {
      ...newsSeed,
      items: [validWithUnsafeThumbnail, ...invalidCases],
    });

    expect(result.status).toBe("ok");
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.id).toBe(original.id);
    expect(result.items[0]).not.toHaveProperty("thumbnailUrl");
    expect(result.rejected).toHaveLength(invalidCases.length + 1);
    expect(result.rejected[0]?.reasons).toContain(
      "thumbnailUrl was dropped because it is not a safe HTTPS URL.",
    );
    expect(result.rejected[1]?.reasons).toContain("title must be a non-empty string.");
    expect(result.rejected[2]?.reasons).toContain(
      "source must be one of: HackerNews, Reddit, Dev.to, HuggingFace, YouTube.",
    );
    expect(result.rejected[3]?.reasons).toContain(
      "upvotes must be null or a finite number.",
    );
    expect(result.rejected[4]?.reasons).toContain(
      "url must be a safe HTTPS URL.",
    );
    expect(result.rejected[5]?.reasons).toContain(
      "publishedAtIso must be a valid ISO date-time string.",
    );
    expect(result.rejected[6]?.reasons).toContain(
      "publishedAtIso must be a valid ISO date-time string.",
    );
    expect(result.rejected[7]?.reasons).toContain(
      "tags must be an array of strings.",
    );
    expect(result.rejected[8]?.reasons).toContain(
      "duplicate id; first occurrence was kept.",
    );
  });

  it("accepts unknown news metrics and summaries without inventing values", () => {
    const original = newsSeed.items[2]!;
    const result = parseDataset("news", {
      ...newsSeed,
      items: [
        { ...original, upvotes: null, commentsCount: null, summary: null },
        { ...original, id: "verified-zeros", upvotes: 0, commentsCount: 0 },
      ],
    });

    expect(result.status).toBe("ok");
    expect(result.rejected).toEqual([]);
    expect(result.items[0]).toMatchObject({
      upvotes: null,
      commentsCount: null,
      summary: null,
    });
    expect(result.items[1]).toMatchObject({ upvotes: 0, commentsCount: 0 });
  });

  it("rejects missing or invalid nullable news values with specific reasons", () => {
    const original = newsSeed.items[2]!;
    const invalidItems = [
      { ...original, upvotes: undefined },
      { ...original, commentsCount: undefined },
      { ...original, upvotes: Number.NaN },
      { ...original, commentsCount: Number.NaN },
      { ...original, summary: "" },
      { ...original, summary: "   " },
    ];
    const result = parseDataset("news", { ...newsSeed, items: invalidItems });

    expect(result.items).toEqual([]);
    expect(result.rejected[0]?.reasons).toContain(
      "upvotes must be null or a finite number.",
    );
    expect(result.rejected[1]?.reasons).toContain(
      "commentsCount must be null or a finite number.",
    );
    expect(result.rejected[2]?.reasons).toContain(
      "upvotes must be null or a finite number.",
    );
    expect(result.rejected[3]?.reasons).toContain(
      "commentsCount must be null or a finite number.",
    );
    for (const rejection of result.rejected.slice(4)) {
      expect(rejection.reasons).toContain(
        "summary must be null or a non-empty string.",
      );
    }
  });

  it("preserves null model values and verified zero prices", () => {
    const result = parseDataset("models", modelsSeed);
    expect(result.status).toBe("ok");

    const unknownValues = result.items.find((item) => item.id === "seed-model-001");
    const zeroValues = result.items.find((item) => item.id === "seed-model-004");
    expect(unknownValues?.arenaElo).toBeNull();
    expect(unknownValues?.codingElo).toBeNull();
    expect(unknownValues?.inputCostPer1M).toBeNull();
    expect(unknownValues?.outputCostPer1M).toBeNull();
    expect(zeroValues?.inputCostPer1M).toBe(0);
    expect(zeroValues?.outputCostPer1M).toBe(0);
  });

  it("accepts an empty items array as a valid dataset", () => {
    const result = parseDataset("news", { ...newsSeed, items: [] });
    expect(result.status).toBe("ok");
    expect(result.items).toEqual([]);
  });
});

describe("parseMetadata", () => {
  it("accepts the seed metadata", () => {
    expect(parseMetadata(metadataSeed)).toEqual({
      status: "ok",
      metadata: metadataSeed,
      error: null,
    });
  });

  it("rejects invalid metadata without throwing", () => {
    expect(parseMetadata(null).status).toBe("invalid");
    expect(parseMetadata("garbage").status).toBe("invalid");
    expect(parseMetadata({ ...metadataSeed, schemaVersion: 99 }).status).toBe(
      "invalid",
    );
  });

  it("accepts optional benchmark provenance when absent, null, or populated", () => {
    const provenance = {
      source: "Fictional benchmark source",
      sourceUrl: "https://example.invalid/benchmark",
      snapshotIso: "2026-01-01T00:00:00Z",
      fetchedIso: "2026-01-02T00:00:00Z",
    };

    expect(parseMetadata(metadataSeed).status).toBe("ok");
    expect(parseMetadata({ ...metadataSeed, benchmarks: null }).status).toBe("ok");
    expect(
      parseMetadata({ ...metadataSeed, benchmarks: provenance }).metadata
        ?.benchmarks,
    ).toEqual(provenance);
  });

  it.each([
    { source: "source", sourceUrl: "javascript:alert(1)", snapshotIso: null, fetchedIso: "2026-01-02T00:00:00Z" },
    { source: "source", sourceUrl: "https://example.invalid/benchmark", snapshotIso: "not-a-date", fetchedIso: "2026-01-02T00:00:00Z" },
    { source: "source", sourceUrl: "https://example.invalid/benchmark", snapshotIso: null, fetchedIso: "not-a-date" },
    { source: "   ", sourceUrl: "https://example.invalid/benchmark", snapshotIso: null, fetchedIso: "2026-01-02T00:00:00Z" },
    [],
  ])("rejects invalid benchmark provenance: %j", (benchmarks) => {
    expect(parseMetadata({ ...metadataSeed, benchmarks }).status).toBe("invalid");
  });
});

describe("data loaders", () => {
  it("reports missing dataset and metadata files without throwing", async () => {
    await useTemporaryDataDirectory();
    await expect(loadDataset("news")).resolves.toMatchObject({
      status: "missing",
      items: [],
    });
    await expect(loadMetadata()).resolves.toMatchObject({
      status: "missing",
      metadata: null,
    });
  });

  it("reports corrupt JSON as invalid without throwing", async () => {
    const dataDirectory = await useTemporaryDataDirectory();
    await writeFile(join(dataDirectory, "news.json"), "{", "utf8");
    await writeFile(join(dataDirectory, "metadata.json"), "{", "utf8");

    await expect(loadDataset("news")).resolves.toMatchObject({
      status: "invalid",
      error: "Dataset file contains invalid JSON.",
    });
    await expect(loadMetadata()).resolves.toMatchObject({
      status: "invalid",
      error: "Metadata file contains invalid JSON.",
    });
  });

  it("isolates loadAll failures so valid files still load", async () => {
    const dataDirectory = await useTemporaryDataDirectory();
    await writeFile(
      join(dataDirectory, "metadata.json"),
      JSON.stringify(metadataSeed),
      "utf8",
    );

    for (const kind of ["news", "builds", "hackathons"] as const) {
      await writeFile(
        join(dataDirectory, `${kind}.json`),
        JSON.stringify(seeds[kind]),
        "utf8",
      );
    }
    await writeFile(join(dataDirectory, "models.json"), "{", "utf8");

    const results = await loadAll();
    expect(results.metadata.status).toBe("ok");
    expect(results.news.status).toBe("ok");
    expect(results.models.status).toBe("invalid");
    expect(results.builds.status).toBe("ok");
    expect(results.hackathons.status).toBe("ok");
  });
});
