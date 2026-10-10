// The voice call window in the real app shell, against fixtures: the spec
// answers every /api request and both sockets itself, so no backend, model or
// account is involved. Chromium's fake microphone and auto-accepted permission
// prompt stand in for the user's. Starts its own dev server on port 4341.
//
//   PLAYWRIGHT_CHANNEL=chrome npx playwright test -c playwright.voice-call.config.ts
import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  testMatch: "voice-call.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 45_000,
  use: {
    baseURL: "http://127.0.0.1:4341",
    channel: process.env.PLAYWRIGHT_CHANNEL ?? "chromium",
    locale: "zh-CN",
    viewport: { width: 1280, height: 820 },
    permissions: ["microphone"],
    launchOptions: {
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
      ],
    },
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4341",
    url: "http://127.0.0.1:4341",
    reuseExistingServer: false,
  },
})
