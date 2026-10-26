import { act, renderHook, waitFor } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLocalStorage } from "@/hooks/useLocalStorage";

const key = "test-preference";
const storageKey = "devnexus:test-preference:v1";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (typeof window !== "undefined") {
    window.localStorage.clear();
  }
});

describe("useLocalStorage", () => {
  it("returns the initial value on first render, then hydrates from storage", async () => {
    window.localStorage.setItem(
      storageKey,
      JSON.stringify({ v: 1, data: "stored value" }),
    );
    const observedValues: string[] = [];

    const { result } = renderHook(() => {
      const state = useLocalStorage(key, "initial value");
      observedValues.push(state[0]);
      return state;
    });

    expect(observedValues[0]).toBe("initial value");
    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    expect(result.current[0]).toBe("stored value");
  });

  it("persists and reads back objects and arrays", async () => {
    const objectHook = renderHook(() => useLocalStorage("object", { count: 0 }));
    await waitFor(() => expect(objectHook.result.current[2].isHydrated).toBe(true));

    act(() => objectHook.result.current[1]({ count: 3 }));
    expect(window.localStorage.getItem("devnexus:object:v1")).toBe(
      JSON.stringify({ v: 1, data: { count: 3 } }),
    );
    objectHook.unmount();

    const objectReadback = renderHook(() =>
      useLocalStorage("object", { count: -1 }),
    );
    await waitFor(() => expect(objectReadback.result.current[0]).toEqual({ count: 3 }));

    const arrayHook = renderHook(() => useLocalStorage("array", ["initial"]));
    await waitFor(() => expect(arrayHook.result.current[2].isHydrated).toBe(true));
    act(() => arrayHook.result.current[1](["one", "two"]));
    expect(JSON.parse(window.localStorage.getItem("devnexus:array:v1") ?? "null")).toEqual({
      v: 1,
      data: ["one", "two"],
    });
  });

  it("supports functional updates without stale state", async () => {
    const { result } = renderHook(() => useLocalStorage("counter", 0));
    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));

    act(() => {
      result.current[1]((previous) => previous + 1);
      result.current[1]((previous) => previous + 1);
    });

    expect(result.current[0]).toBe(2);
    expect(JSON.parse(window.localStorage.getItem("devnexus:counter:v1") ?? "null")).toEqual({
      v: 1,
      data: 2,
    });
  });

  it("falls back without throwing and removes malformed JSON", async () => {
    window.localStorage.setItem(storageKey, "{not json");

    const { result } = renderHook(() => useLocalStorage(key, "initial"));

    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    expect(result.current[0]).toBe("initial");
    expect(window.localStorage.getItem(storageKey)).toBeNull();
  });

  it("falls back and removes entries with a different version", async () => {
    window.localStorage.setItem(storageKey, JSON.stringify({ v: 2, data: "old" }));

    const { result } = renderHook(() =>
      useLocalStorage(key, "initial", { version: 1 }),
    );

    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    expect(result.current[0]).toBe("initial");
    expect(window.localStorage.getItem(storageKey)).toBeNull();
  });

  it("falls back when validation rejects the stored value", async () => {
    window.localStorage.setItem(storageKey, JSON.stringify({ v: 1, data: 42 }));
    const validate = (value: unknown): value is string => typeof value === "string";

    const { result } = renderHook(() =>
      useLocalStorage(key, "initial", { validate }),
    );

    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    expect(result.current[0]).toBe("initial");
    expect(window.localStorage.getItem(storageKey)).toBeNull();
  });

  it("keeps state updated when setItem throws", async () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("storage unavailable");
      });
    const { result } = renderHook(() => useLocalStorage(key, "initial"));
    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));

    expect(() => {
      act(() => result.current[1]("updated"));
    }).not.toThrow();
    expect(result.current[0]).toBe("updated");
    expect(result.current[2].isAvailable).toBe(false);
    expect(setItem).toHaveBeenCalled();
  });

  it("falls back when getItem throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });

    const { result } = renderHook(() => useLocalStorage(key, "initial"));

    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    expect(result.current[0]).toBe("initial");
    expect(result.current[2].isAvailable).toBe(false);
  });

  it("resets state and removes the stored entry", async () => {
    const { result } = renderHook(() => useLocalStorage(key, "initial"));
    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    act(() => result.current[1]("updated"));

    act(() => result.current[2].remove());

    expect(result.current[0]).toBe("initial");
    expect(window.localStorage.getItem(storageKey)).toBeNull();
  });

  it("updates from a storage event for the same key", async () => {
    const { result } = renderHook(() => useLocalStorage(key, "initial"));
    await waitFor(() => expect(result.current[2].isHydrated).toBe(true));
    const event = new StorageEvent("storage", {
      key: storageKey,
      newValue: JSON.stringify({ v: 1, data: "from another tab" }),
      storageArea: window.localStorage,
    });

    act(() => window.dispatchEvent(event));

    expect(result.current[0]).toBe("from another tab");
  });

  it("removes the storage listener on unmount", async () => {
    const removeEventListener = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() => useLocalStorage(key, "initial"));
    unmount();

    expect(removeEventListener).toHaveBeenCalledWith(
      "storage",
      expect.any(Function),
    );
  });

  it("renders on the server without accessing window", () => {
    vi.stubGlobal("window", undefined);
    const Host = () => {
      const [value] = useLocalStorage(key, "server initial");
      return <span>{value}</span>;
    };

    expect(() => renderToString(<Host />)).not.toThrow();
  });
});
