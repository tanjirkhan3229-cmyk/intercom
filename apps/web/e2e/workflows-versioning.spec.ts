import { test, expect, login } from "./support/fixtures";
import { addNode, connect, newWorkflow } from "./support/builder-helpers";

/**
 * P1.6 acceptance #3: publishing a new version leaves in-flight runs pinned to their version. We
 * publish v1, park a run on v1, publish v2, and verify the parked run still reports v1 and the
 * "runs on an older version" indicator appears — while v2 is the active version.
 */
test("in-flight runs stay pinned to their version across a publish", async ({ page, workflow }) => {
  await login(page);
  const wfId = await newWorkflow(page);

  // Minimal valid workflow: trigger → end.
  const end = await addNode(page, "end");
  await addNode(page, "trigger");
  await connect(page, "next", end);

  await expect(page.getByTestId("publish")).toBeEnabled();
  await page.getByTestId("publish").click();
  await expect(page.getByTestId("workflow-status")).toHaveText(/active/i);

  // Park an in-flight run on v1, then publish v2 (a re-publish is a new immutable version).
  workflow.seedRun(wfId, "waiting");
  await expect(page.getByTestId("publish")).toBeEnabled();
  await page.getByTestId("publish").click();

  await page.goto(`/app/workflows/${wfId}/runs`);

  // The parked run is still on v1, and the old-version indicator shows.
  await expect(page.getByTestId("old-version-runs")).toContainText("1");
  const run = page.getByTestId("run-row").first();
  await expect(run).toContainText("v1");
  await expect(run.getByTestId("run-status")).toContainText("waiting");

  // v2 is the active version.
  await expect(
    page.getByTestId("version-row").filter({ hasText: "v2" }).getByTestId("version-active"),
  ).toBeVisible();
});
