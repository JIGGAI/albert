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

test("settings creates and renames a workspace and issues a key into it", async ({ page }) => {
  const name = `Research ${Date.now()}`;
  await page.goto("/settings");
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
