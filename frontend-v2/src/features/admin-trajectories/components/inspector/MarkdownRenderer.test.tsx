import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import MarkdownRenderer from "./MarkdownRenderer"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

describe("MarkdownRenderer", () => {
  it("never loads images referenced by recorded Markdown", () => {
    const { container } = render(
      <MarkdownRenderer
        text={"![chart](https://remote.example/chart.png)\n\n![asset](/api/assets/file_1)"}
      />,
    )
    expect(container.querySelector("img")).toBeNull()
    const blocked = container.querySelectorAll("[data-blocked-image]")
    expect(blocked).toHaveLength(2)
    expect(blocked[0].textContent).toContain("https://remote.example/chart.png")
  })

  it("keeps only absolute http(s) links clickable, without opener or referrer", () => {
    render(
      <MarkdownRenderer
        text={"[docs](https://docs.example/a) [script](javascript:alert(1)) [local](/api/assets/file_1)"}
      />,
    )
    const docs = screen.getByRole("link", { name: "docs" })
    expect(docs.getAttribute("target")).toBe("_blank")
    expect(docs.getAttribute("rel")).toContain("noopener")
    expect(docs.getAttribute("rel")).toContain("noreferrer")
    expect(screen.queryByRole("link", { name: "script" })).toBeNull()
    expect(screen.queryByRole("link", { name: "local" })).toBeNull()
    expect(screen.getByText("local")).toBeTruthy()
  })

  it("drops raw HTML instead of rendering it", () => {
    const { container } = render(
      <MarkdownRenderer
        text={'<img src="https://remote.example/x.png"><script>window.bad = 1</script>text'}
      />,
    )
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("script")).toBeNull()
  })
})
