# @relay/widget

The Messenger widget (P0.6): a Vite + Preact iframe app, a tiny host-page loader, and a stable
bootstrap that resolves which immutable bundle to serve via a rollout pointer.

## Artifacts

| Source | Build output | Role | Budget (gz) |
|---|---|---|---|
| `src/bootstrap.ts` | `dist/boot.js` | **Stable** script the customer embeds; reads the rollout pointer and loads the cohort's versioned bundle | ≤ 5 KB |
| `src/loader.ts` | `dist/relay.js` | Injector: launcher bubble (themable), open/close, unread badge; injects the iframe | ≤ 5 KB |
| `src/app/**` | `dist/index.html` + `dist/assets/*` | The iframe messenger SPA | ≤ 50 KB |

Budgets are enforced by `npm run size` (CI gate).

## How a customer embeds it

```html
<script>
  (function (w) { w.relay = w.relay || function () { (w.relay.q = w.relay.q || []).push(arguments); }; })(window);
  relay('boot', { app_id: 'wrk_...', user: { external_id: '...' }, user_hash: '<HMAC>' });
</script>
<script async src="https://cdn.relay.example/widget/boot.js"></script>
```

`user` + `user_hash` are only needed when the workspace has **identity verification** on:
`user_hash = HMAC-SHA256(workspace_secret, external_id)`. Otherwise the widget resolves a
cookie-scoped **lead** that survives reloads. The messenger UI, theme, office-hours/expected-reply
copy, and conversation list all come from `POST /v0/widget/boot`.

## The messenger

Conversation list ↔ thread, composer (text + attachment affordance), delivery ticks, rating
prompt on close, unread badge (pushed to the launcher). The thread is driven by
`@relay/shared`'s `createRealtimeChannel` (dedupe + jittered reconnect); today it runs in
long-poll mode against `GET /v0/widget/conversations/:id/parts?after=` — the Centrifugo live
transport (push + typing) drops in at `packages/shared/src/realtime.ts` without touching the UI.
Strings route through `src/app/i18n.ts` (i18n-ready). CSP-safe: no inline eval, all assets inlined
or same-versioned-origin.

## Versioned bundles + rollout (RFC-001 §9)

`scripts/publish.mjs` publishes **immutable** bundles and flips a **rollout pointer** — cohort
staging + instant rollback (bundles are never overwritten, so the prior build is always at the
edge). Defaults to a local filesystem target (no AWS needed); point `--target` at S3 for staging/prod.

```
{base}/boot.js                stable bootstrap        (short TTL)
{base}/rollout.json           the pointer             (short TTL)
{base}/v{semver}/relay.js …   immutable versioned     (1y immutable)
```

```bash
npm run build
node scripts/publish.mjs promote 1.0.0                 # → stable
node scripts/publish.mjs canary  1.1.0 --percent 5     # 5% cohort (add --workspace wrk_.. to pin)
node scripts/publish.mjs promote 1.1.0                 # graduate canary → stable
node scripts/publish.mjs rollback                      # instant flip back to the prior stable
node scripts/publish.mjs status
# S3 + CloudFront (invalidates only the short-TTL pointer + bootstrap):
RELAY_CLOUDFRONT_DISTRIBUTION_ID=E123 \
  node scripts/publish.mjs promote 1.1.0 --target s3://relay-cdn/widget
```

Cohort resolution (`src/rollout.ts`) is unit-tested in `src/rollout.test.ts` (`npm test`),
including the rollback-flip invariant.

## Commands

`npm run dev` (app dev server — visit `/?app_id=wrk_..&api_url=http://localhost:8000` to boot
standalone) · `npm run build` (app + loader + bootstrap) · `npm run size` · `npm run typecheck` ·
`npm test`. After a build, `npx serve apps/widget` and open `demo.html` to see the loader embed
the widget.
