import { expect, test } from "@playwright/test";
import { api, headers } from "./seed";

test("backends shows what is in use, that it works, and how to switch", async ({ page, request }) => {
  await request.post(`${api}/v1/memories`, {
    headers,
    data: { content: "Beacon Service uses Beacon Store." },
  });
  await request.post(`${api}/v1/search`, { headers, data: { query: "Beacon Store" } });
  await page.goto("/backends");
  await expect(page.getByRole("heading", { name: "Backends" })).toBeVisible();

  const postgres = page.getByTestId("store-postgres");
  await expect(postgres).toContainText("in use");
  await expect(postgres).toContainText("reachable");
  await expect(postgres.getByTestId("store-entities")).not.toHaveText("0", { timeout: 20_000 });

  const falkor = page.getByTestId("store-falkordb");
  await expect(falkor).toContainText("available, not in use");
  await falkor.getByText("How to turn it on").click();
  await expect(falkor).toContainText("ALBERT_GRAPH_STORE=falkordb");
  await expect(page.getByTestId("store-neo4j")).toContainText("available, not in use");

  await expect(page.getByTestId("builder-regex")).toContainText("in use");
  await expect(page.getByTestId("builder-graphiti")).toContainText("not built yet");

  await page.getByRole("link", { name: "Operations → Graph" }).click();
  await expect(page).toHaveURL(/\/ops\?view=graph/);
  await expect(page.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("ops-graph")).toContainText("is writing to PostgreSQL");
  await expect(page.getByTestId("stat-edges-written")).not.toHaveText("0", { timeout: 20_000 });
  await expect(page.getByTestId("stat-graph-queries")).not.toHaveText("0");
  await expect(page.getByTestId("stat-graph-size")).not.toHaveText("–");
});
