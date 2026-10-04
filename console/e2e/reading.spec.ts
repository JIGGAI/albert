import { expect, test } from "@playwright/test";
import { VOICE, api, headers, seedTopics } from "./seed";

test("reading pane shows a memory in full, formatted, with what it connects to", async ({
  page,
  request,
}) => {
  await seedTopics(request);
  const content = `## Voice rules\n\n${`${VOICE}. `.repeat(25)}\n\n- keep it short\n- no jargon\n`;
  expect(content.length).toBeGreaterThan(2000);
  const created = await request.post(`${api}/v1/memories`, {
    headers,
    data: { subject: "# Voice rules for captions", content },
  });
  const id = (await created.json()).id as string;
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/albert/v1/console/memories/${id}`)).json()).related.length,
      { timeout: 20_000 },
    )
    .toBeGreaterThan(0);

  await page.goto(`/map?memory=${id}`);
  const pane = page.getByTestId("reading-pane");
  await expect(pane).toBeVisible();
  await expect(pane.getByRole("heading", { name: "Voice rules for captions" })).toBeVisible();
  const body = page.getByTestId("reading-content");
  await expect(body.getByRole("heading", { name: "Voice rules" })).toBeVisible();
  await expect(body.getByRole("listitem")).toHaveCount(2);
  expect(((await body.textContent()) ?? "").length).toBeGreaterThan(2000);
  await expect(body).not.toContainText("## Voice rules");

  const related = page.getByTestId("reading-related").getByRole("button");
  await expect(related.first()).toBeVisible();
  await related.first().click();
  await expect(page).not.toHaveURL(new RegExp(`memory=${id}`));
  await expect(page).toHaveURL(/memory=[0-9a-f-]{36}/);
  await expect(pane.getByRole("heading", { name: /Brand voice \d/ })).toBeVisible();
  await expect(page.getByTestId("reading-recalls")).toBeVisible();

  await pane.getByRole("button", { name: "Close" }).click();
  await expect(pane).toHaveCount(0);
  await expect(page).toHaveURL(/\/map$/);
});
