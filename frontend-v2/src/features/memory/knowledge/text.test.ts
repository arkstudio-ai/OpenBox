import { expect, it } from "vitest"
import { matches, plainExcerpt, splitMatches } from "./text"

it("turns page Markdown into a clean preview without dropping real words", () => {
  expect(plainExcerpt("# Guide\n\n## Working together\n\n- Use **weekly** check-ins. [source:s1@2]", "Guide")).toBe(
    "Working together Use weekly check-ins.",
  )
  expect(plainExcerpt("See [[decisions|the decision log]] and [docs](https://example.org).")).toBe(
    "See the decision log and docs.",
  )
  expect(plainExcerpt("| Type | Count |\n| --- | --- |\n| Group | 12 |")).toBe("Type Count Group 12")
  // A sentence that merely starts with the title keeps it; only a repeated heading goes.
  expect(plainExcerpt("云杉项目的负责人是小李。", "云杉项目")).toBe("云杉项目的负责人是小李。")
  expect(plainExcerpt("# 云杉项目\n负责人是小李。", "云杉项目")).toBe("负责人是小李。")
})

it("matches and marks the search term regardless of case", () => {
  expect(matches("Use Shanghai timezone", "shanghai")).toBe(true)
  expect(matches("Use Shanghai timezone", "")).toBe(true)
  expect(matches(null, "x")).toBe(false)
  expect(splitMatches("Ab ab", "ab")).toEqual([
    { text: "Ab", hit: true },
    { text: " ", hit: false },
    { text: "ab", hit: true },
  ])
  expect(splitMatches("no hit", "")).toEqual([{ text: "no hit", hit: false }])
})
