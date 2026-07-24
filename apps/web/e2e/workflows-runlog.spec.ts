import { test, expect, login } from "./support/fixtures";
import { addNode, connect, newWorkflow } from "./support/builder-helpers";

/**
 * P1.6 feature: the run log shows the step timeline with errors and supports re-running from a
 * failed step (idempotent-effect steps only). We publish a workflow, simulate a run that fails at
 * an action, then re-run from that step and watch it complete.
 */
test("run log shows a failed step and supports re-run from that step", async ({ page, workflow }) => {
  await login(page);
  const wfId = await newWorkflow(page);

  const end = await addNode(page, "end");
  const action = await addNode(page, "action");
  await page.getByTestId("reply-body").fill("Thanks — we're on it!");
  await connect(page, "next", end);
  await addNode(page, "trigger");
  await connect(page, "next", action);

  await expect(page.getByTestId("publish")).toBeEnabled();
  await page.getByTestId("publish").click();
  await expect(page.getByTestId("workflow-status")).toHaveText(/active/i);

  // Simulate a run that fails at the action node.
  const runId = workflow.simulate(wfId, { failAtNodeId: action });
  await page.goto(`/app/workflows/${wfId}/runs/${runId}`);

  await expect(page.getByTestId("run-detail-status")).toContainText("failed");
  await expect(page.getByTestId("rerun-from-step")).toBeVisible();

  // Re-run from the failed step → the run completes.
  await page.getByTestId("rerun-from-step").click();
  await expect(page.getByTestId("run-detail-status")).toContainText("completed");
});
