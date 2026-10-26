import { AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { loadAll, type DatasetResult } from "@/lib/data";
import type {
  AIModelItem,
  HackathonItem,
  NewsItem,
  TechBuildItem,
} from "@/types/schema";

function formatUtcTimestamp(isoTimestamp: string): string {
  const date = new Date(isoTimestamp);
  if (Number.isNaN(date.getTime())) {
    return "Unavailable";
  }

  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function formatModelPrice(price: number | null): string {
  return price === null ? "Not available" : `$${price}`;
}

function DatasetCard<T>({
  title,
  result,
  titleForItem,
  detailForItem,
}: {
  title: string;
  result: DatasetResult<T>;
  titleForItem: (item: T) => string;
  detailForItem?: (item: T) => string;
}) {
  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        <Badge variant="outline">{result.status}</Badge>
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-muted-foreground">Items</dt>
          <dd className="mt-1 font-medium">
            {result.status === "ok"
              ? `${result.items.length}`
              : "Unavailable"}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Rejected records</dt>
          <dd className="mt-1 font-medium">{result.rejected.length}</dd>
        </div>
      </dl>
      {result.status !== "ok" ? (
        <p className="mt-5 text-sm text-destructive">Dataset unavailable</p>
      ) : result.items.length > 0 ? (
        <ul className="mt-5 space-y-3">
          {result.items.slice(0, 3).map((item, index) => (
            <li
              className="border-t border-border pt-3 text-sm"
              key={`${titleForItem(item)}-${index}`}
            >
              <p className="font-medium">{titleForItem(item)}</p>
              {detailForItem ? (
                <p className="mt-1 text-muted-foreground">
                  {detailForItem(item)}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-5 text-sm text-muted-foreground">No items available.</p>
      )}
    </section>
  );
}

export default async function Home() {
  const results = await loadAll();
  const isSeedData =
    results.metadata.metadata?.dataOrigin === "seed" ||
    results.news.dataOrigin === "seed" ||
    results.models.dataOrigin === "seed" ||
    results.builds.dataOrigin === "seed" ||
    results.hackathons.dataOrigin === "seed";

  const updatedAt = results.metadata.metadata?.lastUpdatedIso;

  return (
    <main className="min-h-screen px-4 py-10 text-foreground sm:px-8">
      <div className="mx-auto max-w-6xl">
        <header className="mb-8">
          <h1 className="text-3xl font-semibold tracking-tight">DevNexus</h1>
          <p className="mt-2 text-muted-foreground">
            Foundation status — development build
          </p>
        </header>

        {isSeedData ? (
          <div
            className="mb-6 flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm"
            role="alert"
          >
            <AlertTriangle
              aria-hidden="true"
              className="mt-0.5 size-5 shrink-0 text-warning"
            />
            <p className="font-medium">
              Development seed data — not verified live data.
            </p>
          </div>
        ) : null}

        <p className="mb-6 text-sm text-muted-foreground">
          {updatedAt
            ? `${isSeedData ? "Seed generated at" : "Last updated"}: ${formatUtcTimestamp(updatedAt)}`
            : "Metadata unavailable"}
        </p>

        <div className="grid gap-4 sm:grid-cols-2">
          <DatasetCard<NewsItem>
            title="News"
            result={results.news}
            titleForItem={(item) => item.title}
          />
          <DatasetCard<AIModelItem>
            title="Models"
            result={results.models}
            titleForItem={(item) => item.name}
            detailForItem={(item) =>
              `Input: ${formatModelPrice(item.inputCostPer1M)} / Output: ${formatModelPrice(item.outputCostPer1M)} per 1M tokens`
            }
          />
          <DatasetCard<TechBuildItem>
            title="Builds"
            result={results.builds}
            titleForItem={(item) => item.title}
          />
          <DatasetCard<HackathonItem>
            title="Hackathons"
            result={results.hackathons}
            titleForItem={(item) => item.title}
          />
        </div>
      </div>
    </main>
  );
}
