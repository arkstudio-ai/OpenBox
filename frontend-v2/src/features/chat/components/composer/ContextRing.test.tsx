import { afterEach, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { ReactNode } from "react"
import { modelCompactionThreshold } from "../../lib/model"
import { ContextRing } from "./ContextRing"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
  t: (key: string, vars?: Record<string, unknown>) => `${key} ${JSON.stringify(vars ?? {})}`,
}) }))
vi.mock("@/shared/ui/Tooltip", () => ({ Tooltip: ({ label, children }: { label: ReactNode; children: ReactNode }) => <>{children}{label}</> }))
afterEach(cleanup)

it("uses the runtime ceiling and output reserve instead of a fixed 90% warning", () => {
  const { container } = render(<ContextRing used={60000} limit={100000} compactionThreshold={60000} />)
  expect(container.querySelector("circle.stroke-danger")).toBeTruthy()
  expect(screen.getByText(/context.compactAt/).textContent).toContain('"pct":60')
  expect(screen.getByText(/context.compactSoon/)).toBeTruthy()
})

it("does not promise automatic compression when disabled or unknown", () => {
  render(<ContextRing used={95000} limit={100000} />)
  expect(screen.queryByText(/context.compactAt/)).toBeNull()
  expect(screen.queryByText(/context.compactSoon/)).toBeNull()
})

it("switches the ceiling with the selected model and reasoning effort", () => {
  const models = [{ id: "m", name: "Model", compaction: { enabled: true, threshold: 80000, variants: { high: 36000 } } }]
  expect(modelCompactionThreshold("m", models)).toBe(80000)
  expect(modelCompactionThreshold("m", models, "high")).toBe(36000)
  expect(modelCompactionThreshold("missing", models)).toBeUndefined()
  expect(modelCompactionThreshold("m", [{ ...models[0], compaction: { ...models[0].compaction, enabled: false } }])).toBeUndefined()
})
