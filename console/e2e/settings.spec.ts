import { expect, test } from "@playwright/test";

test("settings issues a connection link that redeems once", async ({ page, request }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  const linkCard = page.locator("section", { has: page.getByRole("heading", { name: "One-time connection link" }) });
  await linkCard.getByRole("button", { name: "Create link" }).click();
  const url = await linkCard.getByTestId("secret").textContent();
  expect(url).toMatch(/\/onboard\/[A-Za-z0-9_-]{20,}$/);

  const path = new URL(url!).pathname;
  const api = process.env.ALBERT_API_URL!;
  const manifest = await (await request.get(`${api}${path}/manifest`)).json();
  expect(manifest.capabilities).toEqual(["console.read"]);
  const redeemed = await request.post(manifest.redeem_url.replace(/^https?:\/\/[^/]+/, api), {
    data: { agent_name: "e2e", capabilities: ["console.read"] },
  });
  expect(redeemed.status()).toBe(201);
  const replay = await request.post(`${api}${path}/redeem`, { data: { agent_name: "again" } });
  expect(replay.status()).toBe(410);

  await page.reload();
  await expect(page.getByTestId("links").getByText("used", { exact: true })).toBeVisible();
});

test("settings issues and revokes a least-privilege key", async ({ page }) => {
  await page.goto("/settings");
  const keyCard = page.locator("section", { has: page.getByRole("heading", { name: "Least-privilege API key" }) });
  await keyCard.getByLabel("Name").fill("e2e reader");
  await keyCard.getByRole("button", { name: "Create key" }).click();
  await expect(keyCard.getByTestId("secret")).toHaveText(/^alb_/);

  const row = page.getByTestId("keys").getByRole("row", { name: /e2e reader/ });
  await expect(row).toContainText("memory.read");
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(row).toContainText("revoked");
});
