import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the agent inbox (P0.5 acceptance). Requires the API stack running (`make dev`
 * or `make infra` + API on :8000). The spec seeds workspaces/conversations through the public API
 * and drives the UI. Point at a different API with NEXT_PUBLIC_API_BASE_URL.
 */
const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_PUBLIC_API_BASE_URL: API_BASE_URL },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
