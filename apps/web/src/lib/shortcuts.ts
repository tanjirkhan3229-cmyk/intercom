"use client";

import * as React from "react";

export type ShortcutMap = Record<string, () => void>;

/** True when focus is in a text field — we suppress single-key shortcuts there. */
function inEditable(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null;
  if (!node) return false;
  const tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || node.isContentEditable;
}

/**
 * Register single-key inbox shortcuts (j/k navigate, a assign, s snooze, e close, r reply,
 * n note — RFC P0.5). Keys are ignored while typing, and when a modifier is held (so ⌘R etc.
 * still reach the browser/composer). `enabled` gates the whole map.
 */
export function useShortcuts(map: ShortcutMap, enabled = true): void {
  const mapRef = React.useRef(map);
  mapRef.current = map;

  React.useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (inEditable(e.target)) return;
      const handler = mapRef.current[e.key.toLowerCase()];
      if (handler) {
        e.preventDefault();
        handler();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}
