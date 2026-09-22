import { renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import type { InstalledSkill } from "@/features/skills-center/types"
import { useMineLists } from "./useMineLists"

const skills: InstalledSkill[] = [
  {
    name: "imagegen",
    source: "builtin",
    description: "Generate images",
    builtin_group: "media",
    builtin_group_title: { "zh-CN": "图片与视频", "en-US": "Images and video" },
  },
  { name: "my-prompt", source: "container", description: "Personal writing workflow" },
]

describe("Skill center search", () => {
  it.each(["图片与视频", "IMAGES AND VIDEO", "media"])(
    "finds builtin skills by their category: %s",
    (query) => {
      const { result } = renderHook(() => useMineLists(skills, [], query))
      expect(result.current.skills.map((skill) => skill.name)).toEqual(["imagegen"])
    },
  )

  it("keeps uncategorized personal installs searchable and preserves all sources when cleared", () => {
    const { result, rerender } = renderHook(({ query }) => useMineLists(skills, [], query), {
      initialProps: { query: "writing" },
    })
    expect(result.current.skills.map((skill) => skill.name)).toEqual(["my-prompt"])
    rerender({ query: "" })
    expect(result.current.skills).toEqual(skills)
  })
})
