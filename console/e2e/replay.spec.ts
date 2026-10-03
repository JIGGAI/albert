import { expect, test } from "@playwright/test";

test("replay walks spans and shows fusion", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080";
  const headers = { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` };
  await request.post(`${api}/v1/memories`, {
    headers,
    data: { subject: "Replay fixture", content: "Replay Service uses Replay Store." },
  });
  await new Promise((resolve) => setTimeout(resolve, 2500));
  await request.post(`${api}/v1/search`, { headers, data: { query: "Replay Store" } });
  await page.goto("/");
  await page.getByTestId("trace-row").filter({ hasText: "Replay Store" }).first().click();
  await expect(page.getByRole("heading", { name: /POST \/v1\/search/ })).toBeVisible();
  const rows = page.getByTestId("span-row");
  await expect(rows).toHaveCount(6);
  await expect(rows.nth(2)).toContainText("lexical");
  await page.getByTestId("step-scrubber").fill("5");
  await expect(page.getByTestId("span-detail")).toContainText("fuse");
  await expect(page.getByTestId("fusion-table")).toContainText("lexical");
});
