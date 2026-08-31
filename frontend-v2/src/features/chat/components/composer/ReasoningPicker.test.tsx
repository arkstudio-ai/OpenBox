import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { ReasoningPicker } from "./ReasoningPicker"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

afterEach(cleanup)

describe("ReasoningPicker", () => {
  it("stays hidden for a model without reasoning variants", () => {
    const { container } = render(<ReasoningPicker variants={[]} activeId={undefined} onPick={vi.fn()} />)

    expect(container.innerHTML).toBe("")
  })

  it("offers default and every strength declared by the active model", () => {
    const onPick = vi.fn()
    render(
      <ReasoningPicker
        variants={["low", "medium", "high"]}
        activeId="medium"
        defaultId="medium"
        onPick={onPick}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "reasoning.current" }))
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(4)
    expect(
      screen.getByRole("menuitemradio", { name: "reasoning.level.medium" }).getAttribute("aria-checked"),
    ).toBe("true")

    fireEvent.click(screen.getByRole("menuitemradio", { name: "reasoning.level.high" }))
    expect(onPick).toHaveBeenCalledWith("high")
  })

  it("uses null for default instead of treating it as reasoning off", () => {
    const onPick = vi.fn()
    render(
      <ReasoningPicker
        variants={["off", "low", "high", "max"]}
        activeId="off"
        defaultId="high"
        onPick={onPick}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "reasoning.current" }))
    expect(screen.getByRole("menuitemradio", { name: "reasoning.level.off" })).toBeTruthy()
    fireEvent.click(screen.getByRole("menuitemradio", { name: "reasoning.defaultWithLevel" }))
    expect(onPick).toHaveBeenCalledWith(null)
  })
})
