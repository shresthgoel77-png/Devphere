import { describe, expect, it } from "vitest";
import { isSafeExternalUrl, safeExternalUrlOrNull } from "@/lib/urls";

describe("isSafeExternalUrl", () => {
  it("accepts valid HTTPS URLs", () => {
    expect(isSafeExternalUrl("https://example.com/path?q=1")).toBe(true);
    expect(safeExternalUrlOrNull("https://example.com/path")).toBe(
      "https://example.com/path",
    );
  });

  it.each([
    "javascript:alert(1)",
    "data:text/html,example",
    "http://example.com",
    "file:///tmp/example",
    "blob:https://example.com/id",
    "https://user:password@example.com/path",
    "",
    " https://example.com",
    "https://example.com ",
    "//example.com/path",
    42,
    null,
    `https://example.com/${"a".repeat(2048)}`,
  ])("rejects unsafe value %s", (value) => {
    expect(isSafeExternalUrl(value)).toBe(false);
    expect(safeExternalUrlOrNull(value)).toBeNull();
  });
});
