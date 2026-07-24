import { test, expect, login } from "./support/fixtures";
import { addNode, connect, newWorkflow, selectNode } from "./support/builder-helpers";

/**
 * P1.6 acceptance #2: an invalid graph cannot be published. Missing required config (a
 * route-to-team without a team) and an orphan (unreachable) node both block publish and surface on
 * the node + in the validation panel; fixing them re-enables publish.
 */
test("invalid graph cannot publish; fixing it re-enables publish", async ({ page }) => {
  await login(page);
  await newWorkflow(page);

  const end = await addNode(page, "end");

  const route = await addNode(page, "action");
  await page.getByTestId("action-type").selectOption("route_to_team"); // team_id left empty
  await connect(page, "next", end);

  await addNode(page, "trigger");
  await connect(page, "next", route);

  // Missing required config blocks publish and marks the node.
  await expect(page.getByTestId("publish")).toBeDisabled();
  await expect(page.getByTestId("error-count")).toBeVisible();
  await expect(
    page.locator(`[data-testid=wf-node][data-node-id="${route}"] [data-testid=wf-node-error]`),
  ).toBeVisible();
  await expect(page.getByTestId("validation-panel")).toContainText(/team/i);

  // Fix it → publishable.
  await selectNode(page, route);
  await page.getByTestId("route-team").selectOption("team_x");
  await expect(page.getByTestId("publish")).toBeEnabled();

  // Introduce an orphan (unreachable) node → blocked again.
  await addNode(page, "end");
  await expect(page.getByTestId("publish")).toBeDisabled();
  await expect(page.getByTestId("validation-panel")).toContainText(/can't be reached/i);
});
