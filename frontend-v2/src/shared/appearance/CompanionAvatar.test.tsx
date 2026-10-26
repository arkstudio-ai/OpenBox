import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"
import { CompanionAvatar } from "./CompanionAvatar"
import { companionPortrait, resolveCompanionPreset } from "./companion-presets"

afterEach(cleanup)

it("uses the selected local portrait even when an older custom image remains on the account", () => {
  const { container, rerender } = render(
    <CompanionAvatar appearance={{ preset: "ip-34", image_url: "https://expired.invalid/custom.png" }} />,
  )
  expect(container.querySelector("img")?.getAttribute("src")).toBe(companionPortrait("ip-34"))
  for (const legacy of ["orb", "sprout", "cat"]) {
    rerender(<CompanionAvatar appearance={{ preset: resolveCompanionPreset(legacy), image_url: null }} />)
    expect(container.querySelector("img")?.getAttribute("src")).toBe(companionPortrait(legacy))
  }
  expect(container.querySelector("svg")).toBeNull()
})

it("falls back when a custom URL expires, then accepts a renewed URL without a broken-image loop", () => {
  const { container, rerender } = render(
    <CompanionAvatar appearance={{ preset: "custom", image_url: "https://expired.invalid/old.png" }} />,
  )
  fireEvent.error(container.querySelector("img")!)
  expect(container.querySelector("img")?.getAttribute("src")).toBe(companionPortrait("ip-01"))
  fireEvent.error(container.querySelector("img")!)
  expect(container.querySelector("img")).toBeNull()
  rerender(
    <CompanionAvatar appearance={{ preset: "custom", image_url: "https://renewed.invalid/new.png" }} />,
  )
  expect(container.querySelector("img")?.getAttribute("src")).toBe("https://renewed.invalid/new.png")
})
