// Real app shell with synthetic requests; no account or backend mutations.
import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: "assistant-reminders.spec.ts",
  workers: 1,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:4342",
    channel: process.env.PLAYWRIGHT_CHANNEL ?? "chromium",
    locale: "zh-CN",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4342",
    url: "http://127.0.0.1:4342",
    reuseExistingServer: false,
  },
})
