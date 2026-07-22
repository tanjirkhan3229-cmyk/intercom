# @relay/web

Next.js 15 (App Router, TypeScript, Tailwind, shadcn/ui).

- `src/app/page.tsx` — marketing placeholder, statically generated (SSG).
- `src/app/app/**` — the agent app shell, client-rendered behind auth (CSR). The full
  three-pane inbox lands in P0.5.
- Shared design tokens + the Tailwind preset come from `@relay/shared`; the API client from
  `@relay/sdk-ts`.

## Commands
`npm run dev` · `npm run build` · `npm run lint` · `npm run typecheck` (run from repo root
they resolve through npm workspaces; `make web` runs the dev server).
