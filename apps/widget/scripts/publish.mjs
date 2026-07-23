#!/usr/bin/env node
/**
 * Widget bundle publisher (RFC-001 §9): versioned immutable bundles + a rollout pointer with
 * cohort-staged rollout and instant rollback.
 *
 * CDN layout produced:
 *   {base}/v{semver}/relay.js + index.html + assets/*   immutable, Cache-Control: 1y immutable
 *   {base}/boot.js                                       stable bootstrap,  short TTL
 *   {base}/rollout.json                                  the pointer,       short TTL
 *
 * Usage (run `npm run build` first):
 *   node scripts/publish.mjs promote  [version]  [--target ...]   # upload + make it stable
 *   node scripts/publish.mjs canary   <version>  --percent 5 [--workspace wrk_..]
 *   node scripts/publish.mjs rollback [--target ...]              # flip the pointer back
 *   node scripts/publish.mjs status   [--target ...]
 *
 * --target (or $RELAY_WIDGET_CDN):
 *   local:<dir>        write to a local directory (default: local:./dist/cdn — no AWS needed)
 *   s3://bucket/prefix upload via the AWS CLI; set $RELAY_CLOUDFRONT_DISTRIBUTION_ID to also
 *                      invalidate the (short-TTL) pointer + bootstrap on each publish.
 *
 * Version defaults to package.json's version. Since bundles are immutable, a rollback is only a
 * pointer flip — the prior build is already at every edge, so recovery is a single fast write.
 */
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const DIST = join(ROOT, "dist");
const PKG = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));

const IMMUTABLE = "public, max-age=31536000, immutable";
const SHORT = "public, max-age=60";

// ---- args -------------------------------------------------------------------
const [cmd, ...rest] = process.argv.slice(2);
const flags = {};
const positional = [];
for (let i = 0; i < rest.length; i++) {
  if (rest[i].startsWith("--")) {
    const key = rest[i].slice(2);
    const val = rest[i + 1] && !rest[i + 1].startsWith("--") ? rest[++i] : "true";
    if (key === "workspace") (flags.workspace ??= []).push(val);
    else flags[key] = val;
  } else positional.push(rest[i]);
}
const target = flags.target ?? process.env.RELAY_WIDGET_CDN ?? "local:./dist/cdn";

function die(msg) {
  console.error(`publish: ${msg}`);
  process.exit(1);
}

// ---- target backends (local dir | s3) --------------------------------------
function backend() {
  if (target.startsWith("s3://")) {
    const base = target.replace(/\/+$/, "");
    const aws = (args) => execFileSync("aws", args, { stdio: "inherit" });
    return {
      kind: "s3",
      uploadDir(localDir, key, cacheControl) {
        aws(["s3", "sync", localDir, `${base}/${key}`, "--delete", "--cache-control", cacheControl]);
      },
      uploadFile(localFile, key, cacheControl, contentType) {
        aws(["s3", "cp", localFile, `${base}/${key}`, "--cache-control", cacheControl,
          "--content-type", contentType]);
      },
      readText(key) {
        try {
          return execFileSync("aws", ["s3", "cp", `${base}/${key}`, "-"], { encoding: "utf8" });
        } catch {
          return null;
        }
      },
      invalidate() {
        const id = process.env.RELAY_CLOUDFRONT_DISTRIBUTION_ID;
        if (!id) return;
        aws(["cloudfront", "create-invalidation", "--distribution-id", id,
          "--paths", "/widget/rollout.json", "/widget/boot.js"]);
      },
    };
  }
  const dir = target.startsWith("local:") ? target.slice("local:".length) : target;
  const root = join(ROOT, dir);
  return {
    kind: "local",
    uploadDir(localDir, key) {
      const dest = join(root, key);
      rmSync(dest, { recursive: true, force: true });
      mkdirSync(dest, { recursive: true });
      cpSync(localDir, dest, { recursive: true });
    },
    uploadFile(localFile, key) {
      const dest = join(root, key);
      mkdirSync(dirname(dest), { recursive: true });
      cpSync(localFile, dest);
    },
    readText(key) {
      const p = join(root, key);
      return existsSync(p) ? readFileSync(p, "utf8") : null;
    },
    invalidate() {},
    root,
  };
}

function readRollout(be) {
  const raw = be.readText("rollout.json");
  if (!raw) return { stable: null, previous: null, canary: null, canary_percent: 0, canary_workspaces: [] };
  return JSON.parse(raw);
}

function writeRollout(be, rollout) {
  rollout.updated_at = new Date().toISOString();
  const tmp = join(DIST, ".rollout.json");
  writeFileSync(tmp, JSON.stringify(rollout, null, 2));
  be.uploadFile(tmp, "rollout.json", SHORT, "application/json");
  rmSync(tmp, { force: true });
}

// Stage dist/ minus boot.js into a temp dir to upload as the immutable versioned bundle.
function uploadVersionedBundle(be, version) {
  if (!existsSync(join(DIST, "relay.js"))) die("dist/ not built — run `npm run build` first");
  const staged = join(DIST, `.v${version}`);
  rmSync(staged, { recursive: true, force: true });
  mkdirSync(staged, { recursive: true });
  for (const entry of ["relay.js", "index.html", "assets"]) {
    const src = join(DIST, entry);
    if (existsSync(src)) cpSync(src, join(staged, entry), { recursive: true });
  }
  be.uploadDir(staged, `v${version}`, IMMUTABLE);
  rmSync(staged, { recursive: true, force: true });
}

function uploadBootstrap(be) {
  be.uploadFile(join(DIST, "boot.js"), "boot.js", SHORT, "text/javascript");
}

// ---- commands ---------------------------------------------------------------
function promote(version) {
  const be = backend();
  uploadVersionedBundle(be, version);
  uploadBootstrap(be);
  const prev = readRollout(be);
  writeRollout(be, {
    stable: version,
    previous: prev.stable && prev.stable !== version ? prev.stable : prev.previous ?? null,
    canary: null,
    canary_percent: 0,
    canary_workspaces: [],
  });
  be.invalidate();
  console.log(`promoted v${version} → stable (target ${target})`);
}

function canary(version) {
  if (!version) die("canary requires a version");
  const be = backend();
  uploadVersionedBundle(be, version);
  uploadBootstrap(be);
  const cur = readRollout(be);
  writeRollout(be, {
    stable: cur.stable ?? version,
    previous: cur.previous ?? null,
    canary: version,
    canary_percent: Number(flags.percent ?? 5),
    canary_workspaces: flags.workspace ?? [],
  });
  be.invalidate();
  console.log(`canary v${version} at ${flags.percent ?? 5}% (target ${target})`);
}

function rollback() {
  const be = backend();
  const cur = readRollout(be);
  if (cur.canary) {
    writeRollout(be, { ...cur, canary: null, canary_percent: 0, canary_workspaces: [] });
    be.invalidate();
    console.log(`rolled back: canary v${cur.canary} withdrawn; stable stays v${cur.stable}`);
    return;
  }
  if (!cur.previous) die("no previous stable recorded to roll back to");
  writeRollout(be, { ...cur, stable: cur.previous, previous: cur.stable });
  be.invalidate();
  console.log(`rolled back: stable v${cur.stable} → v${cur.previous} (target ${target})`);
}

function status() {
  console.log(JSON.stringify(readRollout(backend()), null, 2));
}

switch (cmd) {
  case "promote":
    promote(positional[0] ?? PKG.version);
    break;
  case "canary":
    canary(positional[0]);
    break;
  case "rollback":
    rollback();
    break;
  case "status":
    status();
    break;
  default:
    die("usage: publish.mjs <promote|canary|rollback|status> [version] [--target ...] [--percent N] [--workspace wrk_..]");
}
