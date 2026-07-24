import { expect, type Page } from "@playwright/test";

/** The ids of the nodes currently on the canvas (React Flow sets `data-id` per node). */
export async function nodeIds(page: Page): Promise<string[]> {
  return page
    .locator(".react-flow__node")
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-id") ?? "").filter(Boolean));
}

/** Create a new workflow from the list page and land in the builder. Returns the workflow id. */
export async function newWorkflow(page: Page): Promise<string> {
  await page.goto("/app/workflows");
  await page.getByTestId("new-workflow").click();
  await page.waitForURL(/\/app\/workflows\/wfl_/);
  await expect(page.getByTestId("palette")).toBeVisible();
  const match = /\/workflows\/(wfl_[^/?#]+)/.exec(page.url());
  if (!match) throw new Error(`unexpected builder url: ${page.url()}`);
  return match[1] as string;
}

/** Add a node from the palette; it is auto-selected. Returns the new node's id. */
export async function addNode(page: Page, kind: string): Promise<string> {
  const before = await nodeIds(page);
  await page.getByTestId(`palette-${kind}`).click();
  await expect.poll(async () => (await nodeIds(page)).length).toBe(before.length + 1);
  const after = await nodeIds(page);
  const added = after.find((id) => !before.includes(id));
  if (!added) throw new Error(`no new node appeared after adding "${kind}"`);
  // Confirm the inspector switched to the new node before configuring it.
  await expect(page.getByTestId("inspector")).toHaveAttribute("data-selected-node-id", added);
  return added;
}

/** Select a node on the canvas so the inspector targets it. */
export async function selectNode(page: Page, id: string): Promise<void> {
  await page.locator(`[data-testid=wf-node][data-node-id="${id}"]`).click();
  await expect(page.getByTestId("inspector")).toHaveAttribute("data-selected-node-id", id);
}

/** Wire the currently-selected node's `handle` output to a target node (via the inspector). */
export async function connect(page: Page, handle: string, targetId: string): Promise<void> {
  await page.getByTestId(`connect-${handle}`).selectOption(targetId);
}
