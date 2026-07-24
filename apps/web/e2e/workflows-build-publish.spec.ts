import { test, expect, login } from "./support/fixtures";
import { addNode, connect, newWorkflow } from "./support/builder-helpers";

/**
 * P1.6 acceptance #1: build the exact scenario entirely in the UI —
 *   new conversation → (outside office hours?) → collect email → hand to Aide →
 *   (Aide hasn't resolved?) → route to Team X — then publish and view the run log.
 *
 * Nodes are built downstream-first so each connection's target already exists (each new node is
 * auto-selected, so it is configured + wired immediately). Runs against the hermetic mock backend;
 * `E2E_WORKFLOW_BACKEND=real` runs the same spec against the live engine.
 */
test("build the office-hours → collect → Aide → route workflow, publish, and view a run", async ({
  page,
  workflow,
}) => {
  await login(page);
  const wfId = await newWorkflow(page);

  const end = await addNode(page, "end");

  const route = await addNode(page, "action");
  await page.getByTestId("action-type").selectOption("route_to_team");
  await page.getByTestId("route-team").selectOption("team_x");
  await connect(page, "next", end);

  const aideUnresolved = await addNode(page, "condition");
  await page.getByTestId("preset-aide-unresolved").click();
  await connect(page, "true", route);
  await connect(page, "false", end);

  const handToAide = await addNode(page, "action");
  await page.getByTestId("action-type").selectOption("hand_to_aide");
  await connect(page, "next", aideUnresolved);

  const collect = await addNode(page, "collect");
  await page.getByTestId("bot-prompt").fill("What's the best email to reach you?");
  await page.getByTestId("collect-key").fill("email");
  await connect(page, "next", handToAide);

  const officeHours = await addNode(page, "condition");
  await page.getByTestId("preset-office-hours").click();
  await connect(page, "true", collect);
  await connect(page, "false", end);

  await addNode(page, "trigger"); // conversation.created by default
  await connect(page, "next", officeHours);

  // The graph is complete and valid → publishable.
  await expect(page.getByTestId("publish")).toBeEnabled();
  await page.getByTestId("publish").click();
  await expect(page.getByTestId("workflow-status")).toHaveText(/active/i);

  // Execute a run against the published version and view its log.
  const runId = workflow.simulate(wfId);
  await page.goto(`/app/workflows/${wfId}/runs/${runId}`);
  await expect(page.getByTestId("run-timeline")).toBeVisible();
  await expect(page.getByTestId("run-detail-status")).toContainText("completed");
  // The full path executed: trigger + 2 conditions + collect + hand-to-Aide + route + end.
  await expect(page.getByTestId("run-step")).toHaveCount(7);
});
