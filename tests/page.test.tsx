import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Home from "@/app/page";
import { loadAll } from "@/lib/data";

vi.mock("@/lib/data", () => ({
  loadAll: vi.fn(),
}));

const mockLoadAll = vi.mocked(loadAll);

const successfulDataset = <T,>(items: T[]) => ({
  status: "ok" as const,
  items,
  dataOrigin: "seed" as const,
  generatedAtIso: "2026-10-25T00:00:00Z",
  rejected: [],
  error: null,
});

const oneItemResults = {
  metadata: {
    status: "ok" as const,
    metadata: {
      schemaVersion: 1,
      dataOrigin: "seed" as const,
      lastUpdatedIso: "2026-10-25T00:00:00Z",
      lastAttemptIso: null,
      counts: { news: 1, models: 1, builds: 1, hackathons: 1 },
      sources: {},
    },
    error: null,
  },
  news: successfulDataset([
    {
      id: "seed-news-test",
      title: "[SEED] Test news title",
      summary: "Fixture summary",
      url: "https://example.com/seed/news/test",
      source: "HackerNews" as const,
      communityOrAuthor: "Fixture",
      publishedAtIso: "2026-10-24T00:00:00Z",
      upvotes: 0,
      commentsCount: 0,
      tags: ["fixture"],
    },
  ]),
  models: successfulDataset([
    {
      id: "seed-model-test",
      name: "[SEED] Test model",
      provider: "Fixture Provider",
      arenaElo: null,
      codingElo: null,
      contextLength: 8192,
      inputCostPer1M: null,
      outputCostPer1M: null,
      modality: "Text" as const,
      isOpenWeights: false,
      isFreeTierAvailable: false,
      categoryTags: ["fixture"],
      releaseDateIso: "2026-10-01T00:00:00Z",
      openRouterUrl: "https://example.com/seed/models/test",
    },
  ]),
  builds: successfulDataset([
    {
      id: "seed-build-test",
      title: "[SEED] Test build",
      tagline: "Fixture tagline",
      sourcePlatform: "GitHub" as const,
      projectUrl: "https://example.com/seed/builds/test",
      builders: ["Fixture Builder"],
      techStack: ["TypeScript"],
      starsOrLikes: 1,
      createdAtIso: "2026-10-01T00:00:00Z",
    },
  ]),
  hackathons: successfulDataset([
    {
      id: "seed-hackathon-test",
      title: "[SEED] Test hackathon",
      organizer: "Fixture Organizer",
      platform: "Devpost" as const,
      mode: "Online" as const,
      city: "Remote",
      country: "Global",
      startDateIso: "2027-01-01T00:00:00Z",
      deadlineIso: "2026-12-25T00:00:00Z",
      prizePoolText: "Unknown",
      prizeValueNormalizedUsd: null,
      tags: ["fixture"],
      registrationUrl: "https://example.com/seed/hackathons/test",
    },
  ]),
};

beforeEach(() => {
  mockLoadAll.mockResolvedValue(oneItemResults);
});

afterEach(() => {
  cleanup();
});

async function renderHome() {
  render(await Home());
}

describe("foundation page", () => {
  it("shows the seed warning, all four dataset counts, and unknown prices honestly", async () => {
    await renderHome();

    expect(
      screen.getByText("Development seed data — not verified live data."),
    ).toBeInTheDocument();
    for (const name of ["News", "Models", "Builds", "Hackathons"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getAllByText("1", { selector: "dd" })).toHaveLength(4);
    expect(screen.getByText("[SEED] Test model")).toBeInTheDocument();
    expect(
      screen.getByText("Input: Not available / Output: Not available per 1M tokens"),
    ).toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("shows an honest error for an invalid dataset while other sections render", async () => {
    mockLoadAll.mockResolvedValue({
      ...oneItemResults,
      models: {
        status: "invalid",
        items: [],
        dataOrigin: null,
        generatedAtIso: null,
        rejected: [],
        error: "Model data is invalid.",
      },
    });

    await renderHome();

    const modelsHeading = screen.getByRole("heading", { name: "Models" });
    expect(modelsHeading.parentElement?.parentElement).toHaveTextContent(
      "Dataset unavailable",
    );
    expect(screen.getByText("[SEED] Test news title")).toBeInTheDocument();
    expect(screen.getByText("[SEED] Test build")).toBeInTheDocument();
    expect(screen.getByText("[SEED] Test hackathon")).toBeInTheDocument();
  });
});
