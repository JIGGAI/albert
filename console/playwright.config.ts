import { defineConfig } from "@playwright/test";

// The suite owns its servers on dedicated ports. It must never attach to a
// console that is already running: that could be a real deployment.
const consolePort = Number(process.env.ALBERT_E2E_CONSOLE_PORT ?? 18082);

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${consolePort}`, headless: true },
  webServer: {
    command: `npx next dev -p ${consolePort}`,
    port: consolePort,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ALBERT_API_URL: process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080",
      ALBERT_CONSOLE_API_KEY: process.env.ALBERT_CONSOLE_API_KEY ?? "",
    },
  },
});
