/**
 * Design tokens shared by the agent app and the messenger widget.
 *
 * Colors are expressed as HSL channel triplets ("H S% L%") so they can be dropped into
 * CSS custom properties and consumed by Tailwind via `hsl(var(--token))` — the shadcn/ui
 * convention. Values here are the single source of truth for both light and dark themes.
 */

export const radius = {
  sm: "0.25rem",
  md: "0.5rem",
  lg: "0.75rem",
} as const;

export const font = {
  sans: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  mono: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace',
} as const;

/** HSL channel triplets consumed as `hsl(var(--x))`. */
export const lightTheme = {
  background: "0 0% 100%",
  foreground: "222 47% 11%",
  muted: "210 40% 96%",
  "muted-foreground": "215 16% 47%",
  card: "0 0% 100%",
  "card-foreground": "222 47% 11%",
  border: "214 32% 91%",
  input: "214 32% 91%",
  primary: "221 83% 53%",
  "primary-foreground": "210 40% 98%",
  secondary: "210 40% 96%",
  "secondary-foreground": "222 47% 11%",
  accent: "210 40% 96%",
  "accent-foreground": "222 47% 11%",
  destructive: "0 72% 51%",
  "destructive-foreground": "210 40% 98%",
  ring: "221 83% 53%",
} as const;

export const darkTheme = {
  background: "222 47% 11%",
  foreground: "210 40% 98%",
  muted: "217 33% 17%",
  "muted-foreground": "215 20% 65%",
  card: "222 47% 11%",
  "card-foreground": "210 40% 98%",
  border: "217 33% 20%",
  input: "217 33% 20%",
  primary: "217 91% 60%",
  "primary-foreground": "222 47% 11%",
  secondary: "217 33% 17%",
  "secondary-foreground": "210 40% 98%",
  accent: "217 33% 17%",
  "accent-foreground": "210 40% 98%",
  destructive: "0 63% 40%",
  "destructive-foreground": "210 40% 98%",
  ring: "217 91% 60%",
} as const;

export type ThemeTokens = typeof lightTheme;
