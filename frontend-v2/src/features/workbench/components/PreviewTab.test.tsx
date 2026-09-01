import "@testing-library/jest-dom/vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PreviewTab } from "./PreviewTab"

const mocks = vi.hoisted(() => ({
  requestPreviewAccess: vi.fn(),
}))

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock("../api/containers", () => ({
  useRunningContainer: () => ({ id: "box-1", name: "sandbox", status: "running" }),
  useCreateContainer: () => ({ mutate: vi.fn(), isPending: false }),
  useListeningPorts: () => ({
    data: { ports: [{ port: 4173, pid: 1, process: "vite", command: "vite" }] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}))

vi.mock("@/shared/api/containers", () => ({
  containersApi: { requestPreviewAccess: mocks.requestPreviewAccess },
}))

describe("PreviewTab", () => {
  beforeEach(() => {
    mocks.requestPreviewAccess.mockReset()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("seeds preview access before mounting the iframe", async () => {
    mocks.requestPreviewAccess.mockResolvedValue({
      url: "/api/containers/box-1/preview/4173/",
      mode: "sandboxed_same_origin",
    })
    const { container } = render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))

    await waitFor(() => expect(mocks.requestPreviewAccess).toHaveBeenCalledWith("box-1", 4173))
    const frame = container.querySelector("iframe")
    expect(frame).toHaveAttribute("src", "/api/containers/box-1/preview/4173/")
    expect(frame?.getAttribute("src")).not.toContain("_pt")
    expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-popups")
    expect(screen.queryByLabelText("preview.openExternal")).not.toBeInTheDocument()
  })

  it("does not mount a backend-supplied URL outside the preview contract", async () => {
    mocks.requestPreviewAccess.mockResolvedValue({
      url: "https://attacker.example/",
      mode: "sandboxed_same_origin",
    })
    const { container } = render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))

    expect(await screen.findByRole("alert")).toHaveTextContent("preview.authorizeFailed")
    expect(container.querySelector("iframe")).toBeNull()
  })

  it("enables same-origin app APIs and external navigation only on an isolated origin", async () => {
    mocks.requestPreviewAccess.mockResolvedValue({
      url: "https://preview.example.test/api/containers/box-1/preview/4173/",
      mode: "isolated_origin",
    })
    const { container } = render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))

    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull())
    const frame = container.querySelector("iframe")
    expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-same-origin")
    expect(screen.getByLabelText("preview.openExternal")).toBeInTheDocument()
  })

  it("closes an external tab if re-authorization downgrades to same-origin mode", async () => {
    mocks.requestPreviewAccess
      .mockResolvedValueOnce({
        url: "https://preview.example.test/api/containers/box-1/preview/4173/",
        mode: "isolated_origin",
      })
      .mockResolvedValueOnce({
        url: "/api/containers/box-1/preview/4173/",
        mode: "sandboxed_same_origin",
      })
    const replace = vi.fn()
    const close = vi.fn()
    const popup = { location: { replace }, close, opener: window } as unknown as Window
    vi.spyOn(window, "open").mockReturnValue(popup)
    render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))
    const external = await screen.findByLabelText("preview.openExternal")
    fireEvent.click(external)

    await waitFor(() => expect(close).toHaveBeenCalledOnce())
    expect(replace).not.toHaveBeenCalled()
  })

  it("keeps the newest manual authorization when an older request finishes later", async () => {
    let resolveOlder: ((value: { url: string; mode: "sandboxed_same_origin" }) => void) | undefined
    mocks.requestPreviewAccess
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOlder = resolve
          }),
      )
      .mockResolvedValueOnce({
        url: "/api/containers/box-1/preview/3000/",
        mode: "sandboxed_same_origin",
      })
    const { container } = render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))
    const input = screen.getByLabelText("preview.portLabel")
    fireEvent.change(input, { target: { value: "3000" } })
    fireEvent.keyDown(input, { key: "Enter" })

    await waitFor(() =>
      expect(container.querySelector("iframe")).toHaveAttribute("src", "/api/containers/box-1/preview/3000/"),
    )
    await act(async () => {
      resolveOlder?.({
        url: "/api/containers/box-1/preview/4173/",
        mode: "sandboxed_same_origin",
      })
    })
    expect(container.querySelector("iframe")).toHaveAttribute("src", "/api/containers/box-1/preview/3000/")
  })

  it("does not submit Enter again while the current authorization is pending", () => {
    mocks.requestPreviewAccess.mockReturnValue(new Promise(() => undefined))
    render(<PreviewTab />)

    fireEvent.click(screen.getByRole("button", { name: /4173/ }))
    fireEvent.keyDown(screen.getByLabelText("preview.portLabel"), { key: "Enter" })

    expect(mocks.requestPreviewAccess).toHaveBeenCalledTimes(1)
  })
})
