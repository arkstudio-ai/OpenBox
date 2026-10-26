import { afterEach, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { ThinkingTrace } from "./ThinkingTrace"

const rendered = vi.hoisted(() => vi.fn())
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => {
  rendered(text)
  return <p data-testid="reasoning-text">{text}</p>
} }))
afterEach(() => { cleanup(); rendered.mockClear() })

it("does not parse a growing hidden trace and shows its latest text when opened", async () => {
  const text = "A long model reasoning paragraph. ".repeat(2000)
  const { rerender } = render(<ThinkingTrace text={text} streaming />)
  for (let i = 0; i < 10; i++) {
    rerender(<ThinkingTrace text={text + i} streaming />)
  }
  expect(rendered).not.toHaveBeenCalled()
  const toggle = screen.getByRole("button", { expanded: false })
  fireEvent.click(toggle)
  expect((await screen.findByTestId("reasoning-text")).textContent).toBe(text + "9")
  fireEvent.click(toggle)
  rendered.mockClear()
  rerender(<ThinkingTrace text={text + " completed"} streaming={false} />)
  expect(rendered).not.toHaveBeenCalled()
  fireEvent.click(toggle)
  expect((await screen.findByTestId("reasoning-text")).textContent).toBe(text + " completed")
})
