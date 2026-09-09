import { defineConfig } from "@playwright/test"
export default defineConfig({
  testDir: "./e2e",
  testMatch: "admin-skills.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 25_000,
  use: { baseURL: "http://127.0.0.1:4321", channel: "chromium", locale: "zh-CN" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4321",
    url: "http://127.0.0.1:4321",
    reuseExistingServer: false,
  },
})
