import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: "durable-question.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 20_000,
  use: { baseURL: "http://127.0.0.1:4318", channel: "chromium", locale: "zh-CN" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4318",
    url: "http://127.0.0.1:4318",
    reuseExistingServer: false,
  },
})
