import { expect, test } from "@playwright/test";

test("proxy only exposes console reads and the operator search", async ({ request }) => {
  const overview = await request.get("/api/albert/v1/console/overview");
  expect(overview.status()).toBe(200);
  const write = await request.post("/api/albert/v1/memories", { data: { content: "nope" } });
  expect(write.status()).toBe(405);
  const outside = await request.get("/api/albert/v1/memories/00000000-0000-0000-0000-000000000000");
  expect(outside.status()).toBe(404);
  const otherPost = await request.post("/api/albert/v1/console/overview", { data: {} });
  expect(otherPost.status()).toBe(405);
  const escape = await request.get("/api/albert/v1/console/../memories");
  expect([404, 405]).toContain(escape.status());
});
