import { expect, test } from "@playwright/test";

test("proxy only exposes console reads, the operator search and settings", async ({
  request,
  baseURL,
}) => {
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
  const settingsRead = await request.get("/api/albert/v1/settings/credentials");
  expect(settingsRead.status()).toBe(200);
  const otherSettings = await request.get("/api/albert/v1/settings/anything");
  expect(otherSettings.status()).toBe(404);
  const workspaces = await request.get("/api/albert/v1/settings/workspaces");
  expect(workspaces.status()).toBe(200);
  const renameMissing = await request.patch(
    "/api/albert/v1/settings/workspaces/00000000-0000-0000-0000-000000000000",
    { headers: { origin: baseURL! }, data: { name: "nope" } },
  );
  expect(renameMissing.status()).toBe(404);
  const patchMemory = await request.patch(
    "/api/albert/v1/memories/00000000-0000-0000-0000-000000000000",
    { data: { content: "nope" } },
  );
  expect(patchMemory.status()).toBe(405);
  const scope = await request.get("/api/albert/v1/console/scope");
  expect(scope.status()).toBe(200);
  const deleteMemory = await request.delete(
    "/api/albert/v1/memories/00000000-0000-0000-0000-000000000000",
  );
  expect(deleteMemory.status()).toBe(405);
});

test("proxy answers to its own names", async ({ request, baseURL }) => {
  // The default allow-list names localhost, and a published name comes from
  // ALBERT_CONSOLE_ALLOWED_HOSTS (the harness sets console.example.test).
  // A host containing the letter "s" once failed the parser's whitespace check.
  const port = new URL(baseURL!).port;
  for (const host of [`localhost:${port}`, "console.example.test", `console.example.test:${port}`]) {
    const response = await request.get("/api/albert/v1/console/overview", { headers: { host } });
    expect(response.status(), host).not.toBe(421);
  }
});

test("proxy refuses other hosts and cross-origin writes", async ({ request, baseURL }) => {
  // A DNS-rebinding page reaches the console under its own name.
  const rebound = await request.get("/api/albert/v1/console/overview", {
    headers: { host: "attacker.example" },
  });
  expect(rebound.status()).toBe(421);
  const reboundWrite = await request.post("/api/albert/v1/settings/api-keys", {
    headers: { host: "attacker.example:18082", origin: "http://attacker.example:18082" },
    data: { name: "rebound", capabilities: ["memory.read"] },
  });
  expect(reboundWrite.status()).toBe(421);
  // A cross-site form or fetch carries its own Origin, or none from a non-browser.
  const crossSite = await request.post("/api/albert/v1/settings/api-keys", {
    headers: { origin: "https://attacker.example" },
    data: { name: "cross-site", capabilities: ["memory.read"] },
  });
  expect(crossSite.status()).toBe(403);
  const noOrigin = await request.delete(
    "/api/albert/v1/settings/api-keys/00000000-0000-0000-0000-000000000000",
  );
  expect(noOrigin.status()).toBe(403);
  const crossSiteRename = await request.patch(
    "/api/albert/v1/settings/workspaces/00000000-0000-0000-0000-000000000000",
    { headers: { origin: "https://attacker.example" }, data: { name: "taken" } },
  );
  expect(crossSiteRename.status()).toBe(403);
  const workspace = "/api/albert/v1/settings/workspaces/00000000-0000-0000-0000-000000000000";
  const crossSiteDelete = await request.delete(`${workspace}?memories=organization`, {
    headers: { origin: "https://attacker.example" },
  });
  expect(crossSiteDelete.status()).toBe(403);
  const noChoice = await request.delete(workspace, { headers: { origin: baseURL! } });
  expect(noChoice.status()).toBe(422);
  const badTarget = await request.delete(`${workspace}?memories=workspace&to=../memories`, {
    headers: { origin: baseURL! },
  });
  expect(badTarget.status()).toBe(422);
  const missing = await request.delete(`${workspace}?memories=organization`, {
    headers: { origin: baseURL! },
  });
  expect(missing.status()).toBe(404);
  const sameOrigin = await request.post("/api/albert/v1/console/search", {
    headers: { origin: baseURL! },
    data: { query: "anything" },
  });
  expect(sameOrigin.status()).not.toBe(403);
  expect(sameOrigin.status()).not.toBe(421);
});
