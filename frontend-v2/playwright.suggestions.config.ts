import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: "suggestions.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 20_000,
  use: { baseURL: "http://127.0.0.1:4319", channel: "chromium", locale: "zh-CN" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4319",
    url: "http://127.0.0.1:4319",
    reuseExistingServer: false,
  },
})
