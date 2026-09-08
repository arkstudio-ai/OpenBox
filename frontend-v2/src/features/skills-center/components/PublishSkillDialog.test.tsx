import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import { PublishSkillDialog } from "./PublishSkillDialog"

function group(overrides: Partial<SkillGroup> = {}): SkillGroup {
  return {
    id: "my-skill",
    name: "周报助手",
    members: [{ name: "my-skill", source: "container" }],
    isPack: false,
    removable: true,
    origin: "container",
    category: "personal",
    publicationStatus: "unpublished",
    listing: null,
    isOfficial: false,
    ...overrides,
  }
}

function mount(reviewRequired: boolean, target = group()) {
  return render(
    <I18nextProvider i18n={i18n}>
      <PublishSkillDialog
        target={target}
        reviewRequired={reviewRequired}
        busy={false}
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
      />
    </I18nextProvider>,
  )
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("skills")
})

afterEach(cleanup)

// With review on, clicking submits to a queue and nothing is public yet; with
// it off, the click is the publication. Promising the wrong one is the
// difference between "an admin will look at this" and "strangers can read it".
describe("PublishSkillDialog copy", () => {
  it("promises a review when the deployment reviews submissions", () => {
    mount(true)
    expect(screen.getByText(/提交后由管理员审核/)).toBeTruthy()
    expect(screen.getByRole("button", { name: "提交审核" })).toBeTruthy()
    expect(screen.queryByText(/上传后，所有用户都能/)).toBeNull()
  })

  it("keeps the instant-publication copy when review is off", () => {
    mount(false)
    expect(screen.getByText(/上传后，所有用户都能/)).toBeTruthy()
    expect(screen.getByRole("button", { name: "确认上传" })).toBeTruthy()
    expect(screen.queryByText(/提交后由管理员审核/)).toBeNull()
  })

  it("says update rather than upload when a release already exists", () => {
    mount(false, group({ publicationStatus: "published", listing: "listed" }))
    expect(screen.getByRole("button", { name: "确认更新" })).toBeTruthy()
  })

  it("frames a refused release as a resubmission, and repeats why it was refused", () => {
    mount(false, group({ publicationStatus: "published", listing: "rejected", listingNote: "含有密钥" }))
    expect(screen.getByRole("heading", { name: "重新提交到技能商店" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "确认重新提交" })).toBeTruthy()
    expect(screen.getByText("原因：含有密钥")).toBeTruthy()
  })
})
