import { describe, expect, it } from "vitest"
import { SLIDER_STEPS, seqToSlider, sliderMax, sliderToSeq } from "./seekScale"
import { timelineMarks, MERGE_THRESHOLD } from "./timelineMarks"
import type { TimelineItem } from "../../utils/timeline"

describe("seek scale", () => {
  it("maps small ranges one step per event", () => {
    expect(sliderMax("0", "34")).toBe(34)
    expect(seqToSlider("17", "0", "34")).toBe(17)
    expect(sliderToSeq(17, "0", "34")).toBe("17")
  })

  it("names exact seqs beyond Number's safe range", () => {
    const floor = "9007199254740990"
    const ceiling = "9007199254740999"
    expect(sliderToSeq(sliderMax(floor, ceiling), floor, ceiling)).toBe(ceiling)
    expect(sliderToSeq(3, floor, ceiling)).toBe("9007199254740993")
    expect(seqToSlider("9007199254740993", floor, ceiling)).toBe(3)
  })

  it("caps huge ranges at a fixed number of steps and still lands on the ends", () => {
    const ceiling = "900719925474099312345"
    expect(sliderMax("0", ceiling)).toBe(SLIDER_STEPS)
    expect(sliderToSeq(SLIDER_STEPS, "0", ceiling)).toBe(ceiling)
    expect(sliderToSeq(0, "0", ceiling)).toBe("0")
    expect(seqToSlider("99999999999999999999999", "0", ceiling)).toBe(SLIDER_STEPS)
  })
})

describe("timeline marks", () => {
  const item = (
    index: number,
    lane: TimelineItem["lane"],
    status: string | null = "completed",
  ): TimelineItem => ({
    recordId: `tool:${index}`,
    kind: "tool",
    lane,
    start: index / 10_000,
    end: index / 10_000 + 0.00001,
    point: false,
    open: false,
    status,
  })

  it("draws each record when there are few", () => {
    const marks = timelineMarks([item(1, "tool"), item(2, "model")])
    expect(marks.map((mark) => mark.recordIds)).toEqual([["tool:1"], ["tool:2"]])
  })

  it("merges ten thousand records into bounded columns without losing any id or failure", () => {
    const items = Array.from({ length: 10_000 }, (_, index) =>
      item(index, "tool", index === 4321 ? "failed" : "completed"),
    )
    const marks = timelineMarks(items)
    expect(items.length).toBeGreaterThan(MERGE_THRESHOLD)
    expect(marks.length).toBeLessThanOrEqual(360)
    expect(marks.reduce((sum, mark) => sum + mark.recordIds.length, 0)).toBe(10_000)
    expect(marks.find((mark) => mark.recordIds.includes("tool:4321"))?.tone).toBe("danger")
  })
})
