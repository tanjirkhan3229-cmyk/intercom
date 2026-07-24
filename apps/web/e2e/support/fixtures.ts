import { test as base, expect, type Page } from "@playwright/test";
import { installWorkflowMock, type WorkflowMock } from "./workflow-mock";

/**
 * Playwright fixture that installs the hermetic workflow mock for EVERY test (`auto: true`), so a
 * spec need not destructure `workflow` unless it drives runs. Flip to the real P1.5 backend with
 * `E2E_WORKFLOW_BACKEND=real` (mock becomes a no-op; the app talks to the live API). Specs assert
 * only on rendered UI, so they run against either backend unchanged.
 */
export const test = base.extend<{ workflow: WorkflowMock }>({
  workflow: [
    async ({ page }, use) => {
      const backend = process.env.E2E_WORKFLOW_BACKEND ?? "mock";
      const mock = await installWorkflowMock(page, { enabled: backend !== "real" });
      await use(mock);
    },
    { auto: true },
  ],
});

export { expect };

/** Land in the authenticated agent app (mock refresh keeps us signed in). */
export async function login(page: Page): Promise<void> {
  await page.goto("/app/workflows");
  await expect(page.getByTestId("new-workflow")).toBeVisible();
}
