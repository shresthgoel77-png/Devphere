import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { parseDataset, parseMetadata } from "@/lib/data";
import type { DatasetName } from "@/types/schema";

const ingestedDirectory = process.env.DEVNEXUS_INGESTED_DIR;
const datasetNames: DatasetName[] = ["news", "models", "builds", "hackathons"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(path: string): Promise<unknown> {
  return JSON.parse(await readFile(path, "utf8")) as unknown;
}

describe.skipIf(!ingestedDirectory)(
  "ingested cross-language contract (skipped unless DEVNEXUS_INGESTED_DIR is set)",
  () => {
    it("parses all five files, matches metadata counts, and preserves nulls", async () => {
      if (!ingestedDirectory) {
        throw new Error("DEVNEXUS_INGESTED_DIR must be set for this contract.");
      }

      const parsedDatasets = new Map<
        DatasetName,
        { rawItems: unknown[]; items: unknown[] }
      >();

      for (const name of datasetNames) {
        const raw = await readJson(join(ingestedDirectory, `${name}.json`));
        expect(isRecord(raw) && Array.isArray(raw.items)).toBe(true);
        if (!isRecord(raw) || !Array.isArray(raw.items)) {
          throw new Error(`${name}.json does not contain an items array.`);
        }

        const parsed = parseDataset(name, raw);
        expect(parsed.status).toBe("ok");
        if (parsed.status !== "ok") {
          throw new Error(`${name}.json was rejected by parseDataset.`);
        }
        expect(parsed.rejected).toHaveLength(0);
        parsedDatasets.set(name, { rawItems: raw.items, items: parsed.items });
      }

      const metadata = parseMetadata(
        await readJson(join(ingestedDirectory, "metadata.json")),
      );
      expect(metadata.status).toBe("ok");
      if (metadata.status !== "ok" || metadata.metadata === null) {
        throw new Error("metadata.json was rejected by parseMetadata.");
      }

      for (const name of datasetNames) {
        const dataset = parsedDatasets.get(name);
        if (!dataset) {
          throw new Error(`${name}.json was not parsed.`);
        }
        expect(dataset.items).toHaveLength(metadata.metadata.counts[name]);

        dataset.rawItems.forEach((rawItem, index) => {
          const parsedItem = dataset.items[index];
          expect(isRecord(rawItem)).toBe(true);
          expect(isRecord(parsedItem)).toBe(true);
          if (!isRecord(rawItem) || !isRecord(parsedItem)) {
            throw new Error(`${name} item ${index} is not an object.`);
          }
          for (const [field, value] of Object.entries(rawItem)) {
            if (value === null) {
              expect(parsedItem[field], `${name}.${field} must preserve null`).toBeNull();
            }
          }
        });
      }
    });
  },
);
