import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: "media-templates.spec.ts",
  workers: 1,
  timeout: 25_000,
  use: {
    baseURL: "http://127.0.0.1:4347",
    channel: process.env.OPENBOX_BROWSER_CHANNEL || "chromium",
    locale: "zh-CN",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4347",
    url: "http://127.0.0.1:4347",
    reuseExistingServer: false,
  },
})
