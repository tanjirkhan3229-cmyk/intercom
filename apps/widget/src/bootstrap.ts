/**
 * Stable bootstrap — the script a customer embeds (served at `widget/boot.js`, short TTL):
 *
 *   <script>(function(w){w.relay=w.relay||function(){(w.relay.q=w.relay.q||[]).push(arguments)}})(window);
 *     relay('boot',{app_id:'wrk_...'});</script>
 *   <script async src="https://cdn.relay.example/widget/boot.js"></script>
 *
 * It reads the rollout pointer and loads the immutable `v{semver}/relay.js` bundle chosen for
 * this workspace's cohort (RFC-001 §9). Because that bundle is immutable + long-cached, a
 * rollback is just flipping the pointer — the prior build is already at every edge. The
 * customer never re-embeds anything.
 */

import { resolveVersion, type Rollout } from "./rollout";

type Queue = { (...args: unknown[]): void; q?: unknown[][] };

const NS = "relay";
// Capture our own <script> synchronously (document.currentScript is only valid before any await).
const SELF = document.currentScript as HTMLScriptElement | null;

const win = window as unknown as Record<string, Queue | undefined>;
if (!win[NS]) {
  const stub: Queue = (...args: unknown[]) => {
    (stub.q = stub.q || []).push(args);
  };
  win[NS] = stub;
}

/** CDN base dir this bootstrap was served from (…/widget/). */
function cdnBase(): string | null {
  const src = SELF?.src;
  return src ? src.slice(0, src.lastIndexOf("/") + 1) : null;
}

/** The workspace booting, taken from the queued `relay('boot', {app_id})` call, for cohorting. */
function bootAppId(): string | undefined {
  for (const call of win[NS]?.q ?? []) {
    if (call[0] === "boot" && call[1] && typeof call[1] === "object") {
      return (call[1] as { app_id?: string }).app_id;
    }
  }
  return undefined;
}

async function run(): Promise<void> {
  const base = cdnBase();
  if (!base) return;
  let version: string;
  try {
    const resp = await fetch(base + "rollout.json", { cache: "no-cache" });
    if (!resp.ok) return;
    version = resolveVersion((await resp.json()) as Rollout, bootAppId());
  } catch {
    return; // pointer unreachable — fail safe (a customer can pin a versioned URL directly)
  }
  const el = document.createElement("script");
  el.async = true;
  el.src = `${base}v${version}/relay.js`;
  document.head.appendChild(el);
}

void run();
