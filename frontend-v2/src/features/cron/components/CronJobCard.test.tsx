import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { describe, expect, it, vi } from "vitest"
import { CronJobCard } from "@/features/cron/components/CronJobCard"
import type { CronJob } from "@/features/cron/types"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/lib/format", () => ({ formatRelative: () => "relative time" }))
vi.mock("@/features/cron/api/cron", () => ({
  useUpdateCronJob: () => ({ isPending: false, mutate: vi.fn() }),
  useDeleteCronJob: () => ({ isPending: false, mutate: vi.fn() }),
  useRunCronJob: () => ({ isPending: false, mutate: vi.fn() }),
}))

describe("assistant schedules in the existing manager", () => {
  it("opens the main assistant without legacy mutation buttons or raw instructions", () => {
    const job = { id: "schedule", management: "assistant", name: "Daily check",
      task_prompt: "PRIVATE_INSTRUCTIONS", schedule: { kind: "every", every_ms: 600000 } } as CronJob
    render(<MemoryRouter><CronJobCard job={job} onEdit={vi.fn()} /></MemoryRouter>)
    expect(screen.getByText("Daily check")).toBeTruthy()
    expect(screen.getByRole("link").getAttribute("href")).toBe("/app/assistant")
    expect(screen.queryByRole("button")).toBeNull()
    expect(screen.queryByText("PRIVATE_INSTRUCTIONS")).toBeNull()
  })
})
