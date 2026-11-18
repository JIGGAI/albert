import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

test("ESM and CJS package surfaces load", async () => {
  const esm = await import("../dist/esm/index.js");
  const require = createRequire(import.meta.url);
  const cjs = require("../dist/cjs/index.js");
  assert.equal(typeof esm.AlbertClient, "function");
  assert.equal(typeof cjs.AlbertClient, "function");
});

test("browser and server entry points are separated", async () => {
  const browser = await import("../dist/esm/browser.js");
  const server = await import("../dist/esm/server.js");
  assert.equal("ServerAlbertClient" in browser, false);
  assert.equal("BrowserAlbertClient" in server, false);
});
