/**
 * Rollout pointer + cohort resolution (RFC-001 §9: versioned immutable bundles, cohort-staged
 * rollout, instant rollback).
 *
 * The CDN layout the publish script produces:
 *
 *   widget/boot.js          ← stable bootstrap the customer embeds (short TTL)
 *   widget/rollout.json     ← THIS pointer (short TTL) — flip it to roll forward/back instantly
 *   widget/v{semver}/relay.js + index.html + assets/*   ← immutable, long TTL, never overwritten
 *
 * The bootstrap fetches the pointer and calls `resolveVersion` to decide which immutable
 * `v{semver}` bundle to load for the current workspace. Rollback = point `stable` at the prior
 * version and invalidate the pointer; because the versioned bundles are immutable and cached
 * forever, the previous one is already at every edge — the flip is the whole rollback.
 */

export interface Rollout {
  /** The version every workspace gets unless it falls into the canary cohort. */
  stable: string;
  /** The staged version, or null when nothing is being rolled out. */
  canary: string | null;
  /** Percentage (0–100) of workspaces on `canary` (deterministic by workspace id). */
  canary_percent: number;
  /** Workspaces pinned to `canary` regardless of percentage (allow-list for dogfooding). */
  canary_workspaces: string[];
  updated_at?: string;
}

/** Deterministic FNV-1a → 0–99 bucket, so a workspace stays in its cohort across reloads. */
export function bucket(workspaceId: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < workspaceId.length; i++) {
    h ^= workspaceId.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) % 100;
}

/** Resolve the semver a workspace should load. Falls back to `stable` for anything unexpected. */
export function resolveVersion(rollout: Rollout, workspaceId: string | undefined): string {
  const canary = rollout.canary;
  if (canary) {
    if (workspaceId && rollout.canary_workspaces?.includes(workspaceId)) return canary;
    const pct = Number(rollout.canary_percent) || 0;
    if (workspaceId && pct > 0 && bucket(workspaceId) < pct) return canary;
  }
  return rollout.stable;
}
