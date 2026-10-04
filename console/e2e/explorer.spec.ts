import { expect, test } from "@playwright/test";

test("explorer renders a graph and a trace highlights hits", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080";
  const headers = { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` };
  await request.post(`${api}/v1/memories`, {
    headers,
    data: { subject: "Explorer fixture", content: "Explorer Service uses Explorer Store." },
  });
  await new Promise((resolve) => setTimeout(resolve, 2500));
  await request.post(`${api}/v1/search`, { headers, data: { query: "Explorer Store" } });
  await page.goto("/explorer");
  await expect(page.getByRole("heading", { name: "Explorer" })).toBeVisible();
  await expect(page.getByTestId("graph-canvas").locator("canvas")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("graph-node-count")).not.toHaveText("0");
  await page.goto("/");
  await page.getByTestId("trace-row").filter({ hasText: "Explorer Store" }).first().click();
  await expect(page.getByTestId("highlight-legend")).toContainText("lexical");
  await expect(page.getByTestId("highlight-hit-count")).not.toHaveText("0");
  await page.getByTestId("time-slider").fill("0");
  await expect(page.getByTestId("graph-edge-count")).toHaveText("0");
});
