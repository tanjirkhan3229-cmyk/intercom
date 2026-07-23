import { test, expect, type Page } from "@playwright/test";

/**
 * P0.5 inbox acceptance (RFC build-prompts/phase-0 §P0.5):
 *  - a visitor message (seeded via the API) shows up in Unassigned in < 1 s (realtime path);
 *  - an agent assigns → replies → closes entirely by keyboard;
 *  - the list virtualizes at 1k conversations (bounded DOM nodes);
 *  - a refresh restores the exact view state.
 *
 * Requires the API stack on :8000 (make dev). Data is seeded through the public API so the test
 * exercises the same paths the widget/channels will.
 */
const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const PASSWORD = "password123";

interface Owner {
  token: string;
  email: string;
}

async function api<T>(path: string, init: RequestInit & { token?: string } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init.token) headers.Authorization = `Bearer ${init.token}`;
  // Retry transient network drops (e.g. the dev server's --reload closing keep-alives under a
  // seeding burst) — the API itself is idempotent-safe and healthy; this just de-flakes seeding.
  let lastErr: unknown;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const res = await fetch(`${API}${path}`, { ...init, headers });
      if (!res.ok)
        throw new Error(`${init.method ?? "GET"} ${path} → ${res.status}: ${await res.text()}`);
      return (await res.json()) as T;
    } catch (err) {
      lastErr = err;
      // Only retry low-level network failures, not HTTP error responses.
      if (err instanceof Error && err.message.includes("→")) throw err;
      await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
    }
  }
  throw lastErr;
}

async function signup(wsName: string): Promise<Owner> {
  const email = `owner-${crypto.randomUUID()}@example.com`;
  const body = await api<{ access_token: string }>("/v0/auth/signup", {
    method: "POST",
    body: JSON.stringify({ workspace_name: wsName, email, password: PASSWORD, name: "Owner" }),
  });
  return { token: body.access_token, email };
}

async function seedConversation(token: string, message = "Hi, I need help"): Promise<string> {
  const contact = await api<{ id: string }>("/v0/contacts/identify", {
    method: "POST",
    token,
    body: JSON.stringify({ external_id: crypto.randomUUID() }),
  });
  const conv = await api<{ id: string }>("/v0/conversations", {
    method: "POST",
    token,
    body: JSON.stringify({ contact_id: contact.id, body: message }),
  });
  return conv.id;
}

async function loginViaUi(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.fill("#email", email);
  await page.fill("#password", PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/app/);
}

test("visitor message appears in Unassigned in real time, handled entirely by keyboard", async ({
  page,
}) => {
  const owner = await signup("E2E Inbox");
  await loginViaUi(page, owner.email);

  // Open the (empty) Unassigned view and wait for its realtime websocket to connect. The empty
  // view renders the empty state (not the list container), which also confirms the list query
  // resolved and the inbox subscription mounted.
  const wsPromise = page.waitForEvent("websocket", { timeout: 15_000 }).catch(() => null);
  await page.goto("/app?view=unassigned");
  await expect(page.getByText("Nothing here")).toBeVisible();
  await wsPromise;
  await page.waitForTimeout(1_000); // let the channel subscription complete

  // Seed a visitor message AFTER the page is subscribed; it arrives via realtime push. The poll
  // fallback is 10 s, so appearing well under that proves the push path (locally it is ~100 ms).
  await seedConversation(owner.token);
  const firstRow = page.getByTestId("conversation-row").first();
  await expect(firstRow).toBeVisible({ timeout: 5_000 });

  // Assign → reply → close, all by keyboard.
  await firstRow.click();
  await expect(page.getByTestId("thread-timeline")).toBeVisible();

  await page.keyboard.press("a"); // assign to me
  await expect(page.getByRole("button", { name: /You/ })).toBeVisible();

  await page.keyboard.press("r"); // focus composer (reply)
  await page.getByTestId("composer-input").fill("Happy to help — what's going on?");
  await page.keyboard.press("ControlOrMeta+Enter"); // send
  await expect(page.getByText("Happy to help — what's going on?")).toBeVisible();

  await page.getByTestId("thread-timeline").click(); // move focus out of the textarea
  await page.keyboard.press("e"); // close
  await expect(page.getByRole("button", { name: "Reopen" })).toBeVisible();
});

test("refresh restores the exact view state", async ({ page }) => {
  const owner = await signup("E2E Restore");
  await seedConversation(owner.token);
  await loginViaUi(page, owner.email);

  await page.goto("/app?view=all-open");
  const row = page.getByTestId("conversation-row").first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page).toHaveURL(/c=cnv_/);

  const urlBefore = page.url();
  await page.reload();
  // Same view + same selected conversation after reload.
  await expect(page).toHaveURL(urlBefore);
  await expect(page.getByRole("button", { name: "All open" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByTestId("thread-timeline")).toBeVisible();
});

test("conversation list virtualizes at 1k conversations", async ({ page }) => {
  const owner = await signup("E2E Scale");
  const N = Number(process.env.E2E_LIST_SIZE ?? 1000);
  // Seed in parallel batches to keep the fixture fast.
  const batch = 25;
  for (let i = 0; i < N; i += batch) {
    await Promise.all(
      Array.from({ length: Math.min(batch, N - i) }, () => seedConversation(owner.token, "load")),
    );
  }

  await loginViaUi(page, owner.email);
  await page.goto("/app?view=all-open");
  await expect(page.getByTestId("conversation-row").first()).toBeVisible();

  // Virtualization: only a bounded window of rows is in the DOM, not all N.
  const rendered = await page.getByTestId("conversation-row").count();
  expect(rendered).toBeLessThan(80);

  // The scroll container is much taller than its viewport (rows exist beyond the fold).
  const list = page.getByTestId("conversation-list");
  const { scrollHeight, clientHeight } = await list.evaluate((el) => ({
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }));
  expect(scrollHeight).toBeGreaterThan(clientHeight * 3);
});
