import { expect, test } from "@playwright/test";
import { api, headers, seedTopics } from "./seed";

const SHARED = "Organization-wide (shared)";

async function sharedMemory(request: import("@playwright/test").APIRequestContext): Promise<string> {
  const created = await request.post(`${api}/v1/memories`, {
    headers,
    data: {
      subject: "# Shared glossary",
      content: "Albatross is the billing service every team calls",
      scope: "organization",
    },
  });
  expect(created.status()).toBe(201);
  const body = await created.json();
  expect(body.workspace_id).toBeNull();
  return body.id as string;
}

test("the side menu says what this console's key can see", async ({ page }) => {
  await page.goto("/ops");
  // The e2e console runs on the bootstrap administrator key.
  await expect(page.getByTestId("console-scope")).toContainText("Every tenant (admin)");
});

test("shared memories have their own map view and say so in the reading pane", async ({
  page,
  request,
}) => {
  await seedTopics(request);
  const id = await sharedMemory(request);
  await page.goto("/map");
  const picker = page.getByLabel("Workspace");
  await expect(picker.locator("option", { hasText: SHARED })).toHaveCount(1);
  await picker.selectOption({ label: (await picker.locator("option", { hasText: SHARED }).textContent())! });
  await expect(page).toHaveURL(/workspace=shared/);
  await expect(page.getByTestId("map-node-count")).not.toHaveText("0");
  await page.getByLabel("Color by").selectOption("workspace");
  await expect(page.getByTestId("map-legend")).toContainText(SHARED);

  await page.goto(`/map?memory=${id}`);
  await expect(page.getByTestId("reading-workspace")).toContainText(SHARED);
});

test("workspaces creates and renames a workspace that settings issues a key into", async ({
  page,
}) => {
  const name = `Research ${Date.now()}`;
  await page.goto("/ops");
  await page.getByRole("navigation", { name: "Console" }).getByRole("link", { name: "Workspaces" }).click();
  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByRole("link", { name: "Workspaces" })).toHaveAttribute("aria-current", "page");
  const card = page.locator("section", { has: page.getByRole("heading", { name: "Workspaces" }) });
  await card.getByLabel("New workspace name").fill(name);
  await card.getByRole("button", { name: "Create workspace" }).click();
  const row = page.getByTestId("workspaces").getByRole("row", { name: new RegExp(name) });
  await expect(row).toBeVisible();
  await expect(page.getByTestId("workspaces")).toContainText(SHARED);

  await row.getByRole("button", { name: "Rename" }).click();
  await row.getByLabel("Workspace name").fill(`${name} Lab`);
  await row.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("workspaces")).toContainText(`${name} Lab`);

  await page.goto("/settings");
  await expect(page.getByTestId("workspaces")).toHaveCount(0);
  const keyCard = page.locator("section", {
    has: page.getByRole("heading", { name: "Least-privilege API key" }),
  });
  await keyCard.getByLabel("Name").fill("lab reader");
  await keyCard.getByLabel("Scope", { exact: true }).selectOption({ label: `${name} Lab + shared` });
  await keyCard.getByRole("button", { name: "Create key" }).click();
  await expect(keyCard.getByTestId("secret")).toHaveText(/^alb_/);
  const keyRow = page.getByTestId("keys").getByRole("row", { name: /lab reader/ });
  await expect(keyRow).toContainText(`${name} Lab`);
});

test("operations and live can be narrowed to a workspace", async ({ page, request }) => {
  await seedTopics(request);
  await request.post(`${api}/v1/search`, { headers, data: { query: "brand voice" } });
  await page.goto("/ops?view=memory");
  const picker = page.getByLabel("Workspace");
  await expect(picker.locator("option", { hasText: "Default" })).toHaveCount(1);
  await expect(picker.locator("option", { hasText: SHARED })).toHaveCount(1);
  await expect(page.getByTestId("ops-memory")).toContainText("By workspace");

  await page.goto("/live");
  await expect(page.getByTestId("trace-row").first()).toContainText("E2E · Default");
});

test("workspaces deletes a workspace, moving or deleting its memories", async ({
  page,
  request,
  baseURL,
}) => {
  const stamp = Date.now();
  const origin = { origin: baseURL! };
  async function workspaceWithMemory(name: string): Promise<{ id: string; memory: string }> {
    const created = await request.post("/api/albert/v1/settings/workspaces", {
      headers: origin,
      data: { name },
    });
    expect(created.status(), await created.text()).toBe(201);
    const id = (await created.json()).id as string;
    // A key that lives in the new workspace stores the memory there.
    const key = await request.post("/api/albert/v1/settings/api-keys", {
      headers: origin,
      data: { name: `${name} writer`, capabilities: ["memory.write"], workspace_id: id },
    });
    expect(key.status(), await key.text()).toBe(201);
    const stored = await request.post(`${api}/v1/memories`, {
      headers: { Authorization: `Bearer ${(await key.json()).credential}` },
      data: { content: `Kept note for ${name}: the Ibex queue drains nightly` },
    });
    expect(stored.status(), await stored.text()).toBe(201);
    return { id, memory: (await stored.json()).id as string };
  }

  await page.goto("/workspaces");
  const moving = await workspaceWithMemory(`Moving ${stamp}`);
  const doomed = await workspaceWithMemory(`Doomed ${stamp}`);
  await page.reload();
  const table = page.getByTestId("workspaces");

  await table.getByRole("row", { name: new RegExp(`Moving ${stamp}`) }).getByRole("button", { name: "Delete" }).click();
  const panel = page.getByTestId("delete-workspace");
  await expect(panel).toContainText("1 memory");
  await expect(page.getByTestId("delete-workspace-revokes")).toContainText("stop working");
  await panel.getByLabel("Move them to another workspace").check();
  await panel.getByLabel("Move to").selectOption({ label: "Default" });
  await panel.getByRole("button", { name: "Delete workspace" }).click();
  await expect(table).not.toContainText(`Moving ${stamp}`);
  const moved = await request.get(`${api}/v1/memories/${moving.memory}`, { headers });
  expect(moved.status()).toBe(200);
  expect((await moved.json()).workspace_id).not.toBe(moving.id);

  await table.getByRole("row", { name: new RegExp(`Doomed ${stamp}`) }).getByRole("button", { name: "Delete" }).click();
  await panel.getByLabel("Delete them").check();
  const confirm = panel.getByRole("button", { name: "Delete workspace" });
  await expect(confirm).toBeDisabled();
  await panel.getByLabel("Type the workspace name to confirm").fill(`Doomed ${stamp}`);
  await confirm.click();
  await expect(table).not.toContainText(`Doomed ${stamp}`);
  const gone = await request.get(`${api}/v1/memories/${doomed.memory}`, { headers });
  expect(gone.status()).toBe(404);
});
