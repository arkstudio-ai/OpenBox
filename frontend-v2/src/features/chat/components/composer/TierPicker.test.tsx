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
