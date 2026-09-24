import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { TierPicker } from "./TierPicker"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

afterEach(cleanup)

const OPTIONS = [
  { tier: "high" as const, label: "Deep", hint: "Qwen3.8 Max" },
  { tier: "medium" as const, label: "Pro", hint: "Gemini 3.8 Flash" },
  { tier: "low" as const, label: "Fast", hint: "Qwen3.8 Flash" },
]

describe("TierPicker", () => {
  it("names the active tier on the pill and picks by tier", () => {
    const onPick = vi.fn()
    render(
      <TierPicker
        icon={null}
        title="pick"
        options={OPTIONS}
        activeTier="medium"
        fallbackLabel="?"
        onPick={onPick}
      />,
    )

    fireEvent.click(screen.getByTitle("pick"))
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(3)
    expect(screen.getByRole("menuitemradio", { name: /Pro/ }).getAttribute("aria-checked")).toBe("true")
    // What the tier resolves to is in the row, not hidden behind it.
    expect(screen.getByText("Qwen3.8 Max")).toBeTruthy()

    fireEvent.click(screen.getByRole("menuitemradio", { name: /Deep/ }))
    expect(onPick).toHaveBeenCalledWith("high")
    expect(screen.queryByRole("menuitemradio")).toBeNull()
  })

  it("shows the real name when the current choice matches no tier", () => {
    render(
      <TierPicker
        icon={null}
        title="pick"
        options={OPTIONS}
        activeTier={undefined}
        fallbackLabel="DeepSeek V4 Pro"
        onPick={vi.fn()}
      />,
    )
    expect(screen.getByTitle("pick").textContent).toContain("DeepSeek V4 Pro")
  })

  it("offers a tier's chips inline with their price, and picks the pair", () => {
    const onPick = vi.fn()
    render(
      <TierPicker
        icon={null}
        title="pick"
        options={[
          {
            tier: "medium",
            label: "标准",
            hint: "Wan 3.0",
            description: "日常短视频",
            chips: [
              { id: "720p", label: "720p", note: "0.60/s" },
              { id: "1080p", label: "1080p", note: "1.20/s" },
            ],
          },
          {
            tier: "high",
            label: "高清",
            hint: "SD 1080p Pro",
            chips: [{ id: "1080p", label: "1080p", note: "0.50/s" }],
          },
        ]}
        activeTier="medium"
        activeChip="720p"
        fallbackLabel=""
        onPick={onPick}
      />,
    )
    // The pill names the tier and the resolution on it.
    expect(screen.getByTitle("pick").textContent).toContain("标准")
    expect(screen.getByTitle("pick").textContent).toContain("720p")

    fireEvent.click(screen.getByTitle("pick"))
    expect(screen.getByText("日常短视频")).toBeTruthy()
    expect(screen.getByText("1.20/s")).toBeTruthy()
    fireEvent.click(screen.getByText("1080p", { selector: "span" }))
    expect(onPick).toHaveBeenCalledWith("medium", "1080p")

    // Re-opening and picking the tier row keeps the chip already on it.
    fireEvent.click(screen.getByTitle("pick"))
    fireEvent.click(screen.getByRole("menuitemradio", { name: /标准/ }))
    expect(onPick).toHaveBeenLastCalledWith("medium", "720p")

    // A single-chip tier picks that chip from its row.
    fireEvent.click(screen.getByTitle("pick"))
    fireEvent.click(screen.getByRole("menuitemradio", { name: /高清/ }))
    expect(onPick).toHaveBeenLastCalledWith("high", "1080p")
  })

  it("has no fold without a catalogue", () => {
    render(<TierPicker icon={null} title="pick" options={OPTIONS} fallbackLabel="" onPick={vi.fn()} />)
    fireEvent.click(screen.getByTitle("pick"))
    expect(screen.queryByText("tier.more")).toBeNull()
  })

  it("keeps the catalogue behind a fold, and a catalogue pick closes the menu", () => {
    const close = vi.fn()
    render(
      <TierPicker
        icon={null}
        title="pick"
        options={OPTIONS}
        fallbackLabel=""
        onPick={vi.fn()}
        catalogue={(closeMenu) => (
          <button
            type="button"
            onClick={() => {
              close()
              closeMenu()
            }}
          >
            catalogue-row
          </button>
        )}
      />,
    )
    fireEvent.click(screen.getByTitle("pick"))
    expect(screen.queryByText("catalogue-row")).toBeNull()
    fireEvent.click(screen.getByText("tier.more"))
    fireEvent.click(screen.getByText("catalogue-row"))
    expect(close).toHaveBeenCalled()
    expect(screen.queryByText("catalogue-row")).toBeNull()
  })
})

it("offers a fourth fast tier and picks its configured default instead of the first chip", () => {
  const onPick = vi.fn()
  render(
    <TierPicker
      icon={null}
      title="pick"
      options={[
        ...OPTIONS,
        {
          tier: "fast",
          label: "快速",
          hint: "MiniMax-H3-Max-Turbo",
          defaultChip: "768p",
          chips: [
            { id: "480p", label: "480p", note: "0.22/s" },
            { id: "768p", label: "768p", note: "0.34/s" },
          ],
        },
      ]}
      activeTier="medium"
      fallbackLabel=""
      onPick={onPick}
    />,
  )
  fireEvent.click(screen.getByTitle("pick"))
  expect(screen.getAllByRole("menuitemradio")).toHaveLength(4)
  expect(screen.getByText("0.34/s")).toBeTruthy()
  fireEvent.click(screen.getByRole("menuitemradio", { name: /MiniMax-H3-Max-Turbo/ }))
  expect(onPick).toHaveBeenLastCalledWith("fast", "768p")
})
