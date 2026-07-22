# @relay/shared

Design tokens, the Tailwind preset, and shared domain types consumed by `apps/web` and
`apps/widget`. Colors are HSL triplets (shadcn/ui convention) so both themes are driven
from one source (`src/tokens.ts`). Domain unions in `src/types.ts` mirror RFC-002 enums.

Consumed as TypeScript source (frontends list it in `transpilePackages`).
