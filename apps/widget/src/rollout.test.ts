/**
 * Self-check for cohort resolution + the rollback-flip invariant (RFC-001 §9). No framework —
 * a handful of asserts, runnable under any TS runner (tsx / node --experimental-strip-types),
 * matching packages/shared/src/realtime.test.ts.
 */
import { bucket, resolveVersion, type Rollout } from "./rollout.js";

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(`assertion failed: ${msg}`);
}

function base(over: Partial<Rollout> = {}): Rollout {
  return { stable: "1.0.0", canary: null, canary_percent: 0, canary_workspaces: [], ...over };
}

// No canary → everyone on stable.
assert(resolveVersion(base(), "wrk_abc") === "1.0.0", "no canary → stable");

// Allow-listed workspace gets the canary regardless of percentage.
assert(
  resolveVersion(base({ canary: "1.1.0", canary_workspaces: ["wrk_vip"] }), "wrk_vip") === "1.1.0",
  "allow-listed workspace → canary",
);
assert(
  resolveVersion(base({ canary: "1.1.0", canary_workspaces: ["wrk_vip"] }), "wrk_other") ===
    "1.0.0",
  "non-listed workspace with 0% → stable",
);

// 100% canary → everyone on canary; 0% → nobody (besides allow-list).
assert(resolveVersion(base({ canary: "2.0.0", canary_percent: 100 }), "wrk_x") === "2.0.0", "100%");
assert(resolveVersion(base({ canary: "2.0.0", canary_percent: 0 }), "wrk_x") === "1.0.0", "0%");

// bucket() is deterministic and in range.
assert(bucket("wrk_x") === bucket("wrk_x"), "bucket deterministic");
for (const id of ["a", "wrk_1", "wrk_zzz", ""]) {
  const b = bucket(id);
  assert(b >= 0 && b < 100, `bucket in range for ${id}`);
}

// A partial rollout splits the population: some on canary, some on stable.
const partial = base({ canary: "1.5.0", canary_percent: 50 });
const ids = Array.from({ length: 200 }, (_, i) => `wrk_${i}`);
const onCanary = ids.filter((id) => resolveVersion(partial, id) === "1.5.0").length;
assert(onCanary > 0 && onCanary < ids.length, "50% rollout splits the population");

// Rollback flip: promoting to canary then flipping stable back returns everyone to the old build.
const promoted = base({ stable: "2.0.0" });
assert(resolveVersion(promoted, "wrk_x") === "2.0.0", "promoted stable serves new build");
const rolledBack = base({ stable: "1.0.0" }); // pointer flipped back
assert(resolveVersion(rolledBack, "wrk_x") === "1.0.0", "rollback flip serves prior build");

// eslint-disable-next-line no-console
console.log("rollout.test: OK");
