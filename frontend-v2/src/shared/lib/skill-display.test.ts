import { describe, expect, it } from "vitest"
import {
  cleanSkillDisplay,
  skillDisplayName,
  skillDisplayDescription,
  skillMatches,
  skillPackageDisplay,
} from "./skill-display"

const skill = {
  name: "video-production",
  description: "Original discovery",
  display_name: { "zh-CN": "视频制作", "en-US": "Video production" },
  display_description: { "zh-CN": "制作口播视频", "en-US": "Create spoken videos" },
}

describe("Skill display copy", () => {
  it("switches name and summary together without changing the identifier", () => {
    const before = JSON.stringify(skill)
    expect(skillDisplayName(skill, "zh-CN")).toBe("视频制作")
    expect(skillDisplayDescription(skill, "zh-CN")).toBe("制作口播视频")
    expect(skillDisplayName(skill, "en-US")).toBe("Video production")
    expect(skillDisplayDescription(skill, "en-US")).toBe("Create spoken videos")
    expect(JSON.stringify(skill)).toBe(before)
  })
  it("falls back to the other language and then existing copy", () => {
    expect(skillDisplayName({ name: "custom", display_name: { "zh-CN": "我的技能" } }, "en-US")).toBe(
      "我的技能",
    )
    expect(skillDisplayName({ name: "legacy" }, "zh-CN")).toBe("legacy")
    expect(skillDisplayDescription({ name: "legacy", description: "Original" }, "zh-CN")).toBe("Original")
  })
  it.each(["视频", "SPOKEN", "video-production"])(
    "searches both languages and stable identifiers: %s",
    (query) => {
      expect(skillMatches(skill, query)).toBe(true)
    },
  )
  it("merges sparse package overrides and ignores blank form fields", () => {
    expect(
      skillDisplayName(
        skillPackageDisplay({ ...skill, package_display_name: { "zh-CN": "我的视频" } }),
        "en-US",
      ),
    ).toBe("Video production")
    expect(
      cleanSkillDisplay({ display_name: { "zh-CN": " 名称 ", "en-US": " " }, display_description: {} }),
    ).toEqual({ display_name: { "zh-CN": "名称" } })
  })
})
