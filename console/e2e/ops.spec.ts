import { expect, test } from "@playwright/test";
import { api, headers, seedTopics } from "./seed";

test.beforeEach(async ({ request }) => {
  await seedTopics(request);
  await request.post(`${api}/v1/search`, { headers, data: { query: "brand voice sharp confident" } });
  await request.post(`${api}/v1/search`, {
    headers,
    data: { query: "zzzz qqqq xxxx", include_graph: false },
  });
});

test("agents shows who is using memory", async ({ page }) => {
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Console" }).getByRole("link", { name: "Agents" }),
  ).toHaveAttribute("aria-current", "page");
  const rows = page.getByTestId("agent-row");
  await expect(rows.first()).toBeVisible();
  await expect(page.getByTestId("ops-agents")).toContainText("searches");
  await expect(page.getByTestId("stat-searches")).not.toHaveText("0");
});

test("memory shows memory health with links into the map", async ({ page }) => {
  await page.goto("/memory");
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
  await expect(page.getByTestId("stat-memories")).not.toHaveText("0");
  await expect(page.getByTestId("ops-memory")).toContainText("By type");
  const top = page.getByTestId("top-recalled").getByRole("link").first();
  await expect(top).toBeVisible({ timeout: 15_000 });
  await top.click();
  await expect(page).toHaveURL(/\/map\?memory=/);
  await expect(page.getByTestId("reading-pane")).toBeVisible();
});

test("service shows service health", async ({ page }) => {
  await page.goto("/service");
  await expect(page.getByRole("heading", { name: "Service" })).toBeVisible();
  await expect(page.getByTestId("stat-requests")).not.toHaveText("0");
  await expect(page.getByTestId("endpoint-row").filter({ hasText: "POST /v1/search" })).toBeVisible();
  await expect(page.getByTestId("ops-service")).toContainText("hashing");
});

test("retrieval shows retrieval quality and the window can change", async ({ page }) => {
  await page.goto("/retrieval");
  await expect(page.getByTestId("stat-searches")).not.toHaveText("0");
  await expect(page.getByTestId("backend-row")).toHaveCount(3);
  await expect(page.getByTestId("zero-hit-queries")).toContainText("zzzz qqqq xxxx");
  await page.getByLabel("Window").selectOption("168");
  await expect(page).toHaveURL(/hours=168/);
  await expect(page.getByTestId("stat-searches")).not.toHaveText("0");
  await page.getByRole("navigation", { name: "Console" }).getByRole("link", { name: "Service" }).click();
  await expect(page).toHaveURL(/\/service$/);
});

test("old operations links land on the matching page with their filters", async ({ page }) => {
  await page.goto("/ops?view=memory");
  await expect(page).toHaveURL(/\/memory$/);
  await page.goto("/ops?view=retrieval&hours=168");
  await expect(page).toHaveURL(/\/retrieval\?hours=168$/);
  await page.goto("/ops");
  await expect(page).toHaveURL(/\/agents$/);
});

test("the console opens on agents, listed first, with the other views beside it", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  const links = page.getByRole("navigation", { name: "Console" }).getByRole("link");
  await expect(links.first()).toHaveText("Agents");
  await expect(links.first()).toHaveAttribute("aria-current", "page");
  await expect(links).toHaveText([
    "Agents",
    "Memory",
    "Retrieval",
    "Graph",
    "Service",
    "Map",
    "Live",
    "Backends",
    "Workspaces",
    "Settings",
  ]);
  await expect(page.getByRole("link", { name: "Operations" })).toHaveCount(0);
});

test("retrieval lets an operator try a search and see the path it took", async ({ page, request }) => {
  await request.post(`${api}/v1/memories`, {
    headers,
    data: { content: "Probe Service uses Probe Store." },
  });
  await page.goto("/retrieval");
  const probe = page.getByTestId("search-probe");
  await probe.getByTestId("probe-input").fill("brand voice sharp confident");
  await probe.getByRole("button", { name: "Run" }).click();
  await expect(probe.getByTestId("probe-hit-count")).not.toHaveText("0");
  await expect(probe.getByTestId("probe-hit").first()).toContainText("Brand voice");
  const path = probe.getByTestId("probe-path");
  for (const step of ["lexical", "vector", "graph", "fuse"]) await expect(path).toContainText(step);
  await expect(path).toContainText("kept");
  await expect(probe.getByTestId("probe-total")).toContainText("ms");

  await probe.getByRole("checkbox", { name: "Include the graph" }).uncheck();
  await probe.getByRole("button", { name: "Run" }).click();
  await expect(path.getByRole("listitem")).toHaveCount(3);

  await probe.getByRole("link", { name: "Open the full replay" }).click();
  await expect(page.getByRole("heading", { name: /POST \/v1\/console\/search/ })).toBeVisible({
    timeout: 15_000,
  });
});
