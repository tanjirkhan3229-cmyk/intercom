#!/usr/bin/env node
/**
 * Bundle-size budget gate (RFC-001 §9 — bad JS shipped to millions of pages is a real
 * blast radius, so size is a hard CI gate).
 *
 *   - iframe app bundle:  ≤ 50 KB gzipped  (the phase-0 budget)
 *   - host-page loader:   ≤ 5 KB gzipped   (P0.6: the snippet on millions of pages)
 *   - stable bootstrap:   ≤ 5 KB gzipped   (P0.6: reads the rollout pointer)
 *
 * Run after `npm run build`. Exits non-zero if any budget is exceeded.
 */
import { gzipSync } from "node:zlib";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// fileURLToPath (not URL.pathname) so paths with spaces resolve correctly.
const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const APP_BUDGET = 50 * 1024;
const LOADER_BUDGET = 5 * 1024;
const BOOTSTRAP_BUDGET = 5 * 1024;

function gzBytes(file) {
  return gzipSync(readFileSync(file)).length;
}

function fmt(n) {
  return `${(n / 1024).toFixed(1)} KB`;
}

if (!existsSync(DIST)) {
  console.error("check-size: dist/ not found — run `npm run build` first");
  process.exit(1);
}

const assetsDir = join(DIST, "assets");
let appGz = 0;
if (existsSync(assetsDir)) {
  for (const f of readdirSync(assetsDir)) {
    if (f.endsWith(".js")) appGz += gzBytes(join(assetsDir, f));
  }
}

const loaderPath = join(DIST, "relay.js");
const loaderGz = existsSync(loaderPath) ? gzBytes(loaderPath) : 0;

const bootstrapPath = join(DIST, "boot.js");
const bootstrapGz = existsSync(bootstrapPath) ? gzBytes(bootstrapPath) : 0;

let failed = false;
const check = (label, gz, budget) => {
  const ok = gz <= budget;
  failed = failed || !ok;
  console.log(`${ok ? "✓" : "✗"} ${label}: ${fmt(gz)} gz (budget ${fmt(budget)})`);
};

check("iframe app", appGz, APP_BUDGET);
check("loader (relay.js)", loaderGz, LOADER_BUDGET);
check("bootstrap (boot.js)", bootstrapGz, BOOTSTRAP_BUDGET);

if (failed) {
  console.error("check-size: FAIL — bundle over budget");
  process.exit(1);
}
console.log("check-size: OK");
