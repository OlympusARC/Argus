"use client";

import { useCallback, useSyncExternalStore } from "react";

import type { Job } from "./taxonomy";

/**
 * Which roles you have applied to, and which you have hidden.
 *
 * In localStorage rather than the database, for one blunt reason: the jobs
 * table is disposable. It has been truncated and refilled repeatedly, so a
 * column on `jobs` would be wiped each time. Anything durable would need its
 * own table that the rebuild deliberately skips.
 *
 * The second reason is that the dashboard has no accounts. A row in the
 * database is the truth for everyone who opens the page; a row in
 * localStorage is the truth for this browser, which is what "I applied to
 * this" means when there is one of you.
 *
 * The cost is real: clear the browser and it is gone, and it does not follow
 * you to another machine.
 */
const KEY = "argus:job-state:v1";

export type JobState = { applied: Set<string>; dismissed: Set<string> };

const EMPTY: JobState = { applied: new Set(), dismissed: new Set() };

export function jobKey(j: Job): string {
  return `${j.ats}:${j.slug}:${j.external_id}`;
}

/**
 * useSyncExternalStore compares snapshots by identity, so parsing on every
 * call would loop forever. The parse is cached against the raw string and
 * only redone when the stored text actually changes.
 */
let cachedRaw: string | null = null;
let cachedValue: JobState = EMPTY;

function snapshot(): JobState {
  const raw = window.localStorage.getItem(KEY);
  if (raw === cachedRaw) return cachedValue;
  cachedRaw = raw;
  try {
    const parsed = JSON.parse(raw ?? "{}") as { applied?: string[]; dismissed?: string[] };
    cachedValue = {
      applied: new Set(parsed.applied ?? []),
      dismissed: new Set(parsed.dismissed ?? []),
    };
  } catch {
    /* a corrupt entry costs you your marks, not the page */
    cachedValue = EMPTY;
  }
  return cachedValue;
}

/**
 * The server has none of this, so it renders the empty state and the client
 * reconciles on hydration. Returning a different value here would make the
 * two disagree and React would discard the tree.
 */
function serverSnapshot(): JobState {
  return EMPTY;
}

const listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  listeners.add(fn);
  /**
   * Another tab writing the same key fires `storage` here but not in the tab
   * that wrote it, which is why local writes notify explicitly below.
   */
  window.addEventListener("storage", fn);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("storage", fn);
  };
}

function write(next: JobState) {
  try {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ applied: [...next.applied], dismissed: [...next.dismissed] }),
    );
  } catch {
    /* private browsing, or a full quota */
  }
  listeners.forEach((fn) => fn());
}

export function useJobState() {
  const state = useSyncExternalStore(subscribe, snapshot, serverSnapshot);

  const toggleApplied = useCallback(
    (key: string) => {
      const applied = new Set(state.applied);
      if (applied.has(key)) applied.delete(key);
      else applied.add(key);
      write({ applied, dismissed: state.dismissed });
    },
    [state],
  );

  const dismiss = useCallback(
    (key: string) => {
      const dismissed = new Set(state.dismissed);
      dismissed.add(key);
      write({ applied: state.applied, dismissed });
    },
    [state],
  );

  const restoreAll = useCallback(() => {
    write({ applied: state.applied, dismissed: new Set() });
  }, [state]);

  return {
    applied: state.applied,
    dismissed: state.dismissed,
    toggleApplied,
    dismiss,
    restoreAll,
  };
}
