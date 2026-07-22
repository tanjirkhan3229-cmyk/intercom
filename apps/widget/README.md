# @relay/widget

The Messenger widget: a Vite + Preact iframe app plus a tiny host-page loader.

- `src/loader.ts` → `dist/relay.js` — the ≤5 KB snippet customers embed; injects the iframe.
- `src/app/**` → `dist/` — the iframe SPA (placeholder; full messenger in P0.6).
- Bundle-size budget (**50 KB gz** app, 10 KB gz loader) is enforced by `npm run size`.

## Commands
`npm run dev` (app dev server) · `npm run build` (app + loader) · `npm run size` ·
`npm run typecheck`. Open `demo.html` after a build to see the loader embed the widget.
