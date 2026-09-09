import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["video-results.spec.ts", "direct-video.spec.ts", "chunk-recovery.spec.ts"],
  workers: 1,
  retries: 0,
  timeout: 20_000,
  use: { baseURL: "http://127.0.0.1:4327", channel: "chromium", locale: "zh-CN" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4327",
    url: "http://127.0.0.1:4327",
    reuseExistingServer: false,
  },
})
