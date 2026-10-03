import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  use: { baseURL: "http://127.0.0.1:8082", headless: true },
  webServer: {
    command: "npm run dev",
    port: 8082,
    reuseExistingServer: true,
    timeout: 120_000,
    env: {
      ALBERT_API_URL: process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080",
      ALBERT_CONSOLE_API_KEY: process.env.ALBERT_CONSOLE_API_KEY ?? "",
    },
  },
});
