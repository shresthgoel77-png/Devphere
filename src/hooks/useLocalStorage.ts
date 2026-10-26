"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type StoredEnvelope = {
  v: number;
  data: unknown;
};

function parseStoredValue<T>(
  rawValue: string | null,
  version: number,
  validate?: (value: unknown) => value is T,
): { valid: true; value: T } | { valid: false } {
  if (rawValue === null) {
    return { valid: false };
  }

  try {
    const parsed: unknown = JSON.parse(rawValue);
    if (typeof parsed !== "object" || parsed === null) {
      return { valid: false };
    }

    const envelope = parsed as StoredEnvelope;
    if (
      envelope.v !== version ||
      !Object.prototype.hasOwnProperty.call(envelope, "data") ||
      typeof envelope.data === "function" ||
      (validate !== undefined && !validate(envelope.data))
    ) {
      return { valid: false };
    }

    return { valid: true, value: envelope.data as T };
  } catch {
    return { valid: false };
  }
}

export function useLocalStorage<T>(
  key: string,
  initialValue: T,
  options?: { version?: number; validate?: (value: unknown) => value is T },
): readonly [
  value: T,
  setValue: (next: T | ((prev: T) => T)) => void,
  controls: { remove: () => void; isHydrated: boolean; isAvailable: boolean },
] {
  const version = options?.version ?? 1;
  const validate = options?.validate;
  const storageKey = `devnexus:${key}:v${version}`;
  const valueRef = useRef(initialValue);

  const [value, setStateValue] = useState(initialValue);
  const [isHydrated, setIsHydrated] = useState(false);
  const [isAvailable, setIsAvailable] = useState(true);

  const updateValue = useCallback((nextValue: T) => {
    valueRef.current = nextValue;
    setStateValue(nextValue);
  }, []);

  const remove = useCallback(() => {
    updateValue(initialValue);
    try {
      window.localStorage.removeItem(storageKey);
      setIsAvailable(true);
    } catch {
      setIsAvailable(false);
    }
  }, [initialValue, storageKey, updateValue]);

  const setValue = useCallback(
    (next: T | ((prev: T) => T)) => {
      const nextValue =
        typeof next === "function"
          ? (next as (prev: T) => T)(valueRef.current)
          : next;

      if (nextValue === undefined || typeof nextValue === "function") {
        remove();
        return;
      }

      updateValue(nextValue);
      try {
        const serialized = JSON.stringify({ v: version, data: nextValue });
        if (serialized === undefined) {
          window.localStorage.removeItem(storageKey);
          setIsAvailable(true);
          return;
        }
        window.localStorage.setItem(storageKey, serialized);
        setIsAvailable(true);
      } catch {
        setIsAvailable(false);
      }
    },
    [remove, storageKey, updateValue, version],
  );

  useEffect(() => {
    let active = true;

    const readValue = (rawValue: string | null) => {
      const parsed = parseStoredValue(rawValue, version, validate);
      if (parsed.valid) {
        updateValue(parsed.value);
        return;
      }

      updateValue(initialValue);
      if (rawValue !== null) {
        try {
          window.localStorage.removeItem(storageKey);
          if (active) {
            setIsAvailable(true);
          }
        } catch {
          if (active) {
            setIsAvailable(false);
          }
        }
      }
    };

    const handleStorage = (event: StorageEvent) => {
      if (event.key === null) {
        readValue(null);
        return;
      }

      if (event.key !== storageKey) {
        return;
      }
      readValue(event.newValue);
    };

    queueMicrotask(() => {
      if (!active) {
        return;
      }

      try {
        const storedValue = window.localStorage.getItem(storageKey);
        readValue(storedValue);
        setIsAvailable(true);
      } catch {
        setIsAvailable(false);
        updateValue(initialValue);
      } finally {
        setIsHydrated(true);
      }
    });

    window.addEventListener("storage", handleStorage);
    return () => {
      active = false;
      window.removeEventListener("storage", handleStorage);
    };
  }, [initialValue, storageKey, updateValue, validate, version]);

  return [value, setValue, { remove, isHydrated, isAvailable }] as const;
}
