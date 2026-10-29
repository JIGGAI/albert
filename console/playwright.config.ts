import { defineConfig } from "@playwright/test";

// The suite owns its servers on dedicated ports. It must never attach to a
// console that is already running: that could be a real deployment.
const consolePort = Number(process.env.ALBERT_E2E_CONSOLE_PORT ?? 18082);

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  // Stream recovery is bounded and tested in the application. A persistent
  // failure must fail this suite on its first run with retained diagnostics.
  retries: 0,
  workers: 1,
  outputDir: "test-results",
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${consolePort}`,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: process.env.ALBERT_E2E_EXTERNAL_SERVER
    ? undefined
    : {
        command: `npx next dev -p ${consolePort}`,
        port: consolePort,
        reuseExistingServer: false,
        timeout: 120_000,
        env: {
          ALBERT_API_URL: process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080",
          ALBERT_CONSOLE_API_KEY: process.env.ALBERT_CONSOLE_API_KEY ?? "",
          ALBERT_CONSOLE_ALLOWED_HOSTS: "localhost,127.0.0.1,[::1],console.example.test",
        },
      },
});
