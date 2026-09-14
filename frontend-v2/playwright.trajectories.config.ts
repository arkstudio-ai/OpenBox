// Admin trajectory viewer — fixture browser tests and scale evidence.
// Runs against the Vite dev server already up on 127.0.0.1:3101 and never
// starts, restarts or reuses another one (no webServer). Every /api and /ws
// request is answered by e2e/helpers/trajectory-server.ts from real projector
// state, so these runs are fixture evidence for the production components —
// not proof of the backend's authorization, storage or API.
//
//   npx playwright test -c playwright.trajectories.config.ts
import { defineConfig } from "@playwright/test"

const EVIDENCE = "test-results/trajectory-claude/qa-playwright"

export default defineConfig({
  testDir: "./e2e",
  testMatch: /trajectories(-[a-z]+)?\.spec\.ts$/,
  // Playwright empties outputDir on every run; screenshots meant to be kept are
  // written next to it by the specs instead.
  outputDir: `${EVIDENCE}/artifacts`,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["json", { outputFile: `${EVIDENCE}/report.json` }]],
  use: {
    baseURL: process.env.TRAJECTORY_FIXTURE_ORIGIN ?? "http://127.0.0.1:3101",
    channel: "chromium",
    viewport: { width: 1600, height: 1000 },
    screenshot: "only-on-failure",
    trace: "off",
    serviceWorkers: "block",
  },
})
