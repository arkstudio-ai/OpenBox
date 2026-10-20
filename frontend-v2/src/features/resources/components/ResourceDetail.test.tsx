import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { ResourceDetail } from "./ResourceDetail"
import type { Resource } from "../types"

vi.mock("react-i18next", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("./ResourcePreview", () => ({ ResourcePreview: () => null }))
const save = vi.hoisted(() => vi.fn())
vi.mock("../api/resources", () => ({
  downloadResource: vi.fn(),
  useDescribeResource: () => ({ mutate: save, isPending: false, isError: false }),
  useRecognizeResource: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const asset: Resource = { id: "image", name: "IMG_0365.png", description: "", descriptionState: "processing",
  mime: "image/png", size: 100, kind: "image", source: "user", projectId: null, sessionId: null,
  status: "ready", createdAt: "2026-10-19T00:00:00Z", sandboxPath: "/workspace/IMG_0365.png", url: "" }

it("fills the live AI description without a page refresh", () => {
  const { rerender } = render(<ResourceDetail resource={asset} onDelete={vi.fn()} />)
  expect(screen.getByText("description.recognizing")).toBeTruthy()
  rerender(<ResourceDetail resource={{ ...asset, description: "红色圆形", descriptionState: "ready" }} onDelete={vi.fn()} />)
  expect((screen.getByLabelText("description.label") as HTMLTextAreaElement).value).toBe("红色圆形")
})

it("does not erase a person's unsaved typing when recognition completes", () => {
  const { rerender } = render(<ResourceDetail resource={asset} onDelete={vi.fn()} />)
  fireEvent.change(screen.getByLabelText("description.label"), { target: { value: "蛋糕店开业用的图" } })
  rerender(<ResourceDetail resource={{ ...asset, description: "红色圆形", descriptionState: "ready" }} onDelete={vi.fn()} />)
  expect((screen.getByLabelText("description.label") as HTMLTextAreaElement).value).toBe("蛋糕店开业用的图")
  fireEvent.click(screen.getByText("description.save"))
  expect(save).toHaveBeenCalledWith({ id: "image", description: "蛋糕店开业用的图" }, expect.any(Object))
})
