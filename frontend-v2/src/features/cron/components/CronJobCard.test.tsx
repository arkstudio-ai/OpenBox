import "@testing-library/jest-dom/vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { CronJob } from "@/features/cron/types"
import { CronJobCard } from "./CronJobCard"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock("@/features/cron/api/cron", () => ({
  useUpdateCronJob: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteCronJob: () => ({ mutate: vi.fn(), isPending: false }),
  useRunCronJob: () => ({ mutate: vi.fn(), isPending: false }),
}))

vi.mock("@/features/cron/components/CronRunList", () => ({
  CronRunList: () => null,
}))

const physicalPath = "/workspace/openbox/users/u-secret/projects/p-secret-demo"
const job: CronJob = {
  id: "job-1",
  user_id: "u-secret",
  project_id: "p-secret",
  session_id: null,
  name: "Unicode path check",
  description: "",
  enabled: true,
  schedule: { kind: "every", every_ms: 3_600_000 },
  task_prompt: "Inspect 资料/你好😀.txt",
  agent: "default",
  model: null,
  timeout_seconds: 300,
  delivery: {},
  delete_after_run: false,
  next_run_at: null,
  last_run_at: null,
  last_status: null,
  last_error: null,
  last_duration_ms: null,
  consecutive_errors: 0,
  total_runs: 0,
  total_successes: 0,
  total_failures: 0,
  running: false,
  created_at: null,
  updated_at: null,
  project_directory: physicalPath,
}

afterEach(cleanup)

describe("CronJobCard project location", () => {
  it("shows the project name and relative root without exposing the physical namespace", () => {
    render(<CronJobCard job={job} projectName="浏览器验收" onEdit={vi.fn()} />)

    expect(screen.getByTestId("cron-project-root")).toHaveTextContent("浏览器验收 · .")
    expect(document.body).not.toHaveTextContent("/workspace/openbox/users")
  })

  it("falls back to relative-root semantics when the project name is unavailable", () => {
    render(<CronJobCard job={job} onEdit={vi.fn()} />)

    expect(screen.getByTestId("cron-project-root")).toHaveTextContent("job.projectRoot · .")
    expect(document.body).not.toHaveTextContent("p-secret-demo")
  })
})
