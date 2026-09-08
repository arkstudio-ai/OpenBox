import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import { SkillGroupsSection } from "./SkillGroupsSection"

function group(overrides: Partial<SkillGroup> = {}): SkillGroup {
  return {
    id: overrides.id ?? "my-skill",
    name: overrides.name ?? overrides.id ?? "my-skill",
    members: [{ name: overrides.id ?? "my-skill", source: "container" }],
    isPack: false,
    removable: true,
    origin: "container",
    category: "personal",
    publicationStatus: "published",
    listing: "listed",
    isOfficial: false,
    ...overrides,
  }
}

const actions = {
  uninstallSkill: vi.fn(),
  fixDependencies: vi.fn(),
  publishSkill: vi.fn(),
  withdrawSkill: vi.fn(),
  downloadSkill: vi.fn(),
  busy: false,
}

function mount(groups: SkillGroup[]) {
  return render(
    <I18nextProvider i18n={i18n}>
      <SkillGroupsSection title="个人" groups={groups} unmetFor={() => []} actions={actions} />
    </I18nextProvider>,
  )
}

/** The row a given group rendered into, so sibling rows cannot answer for it. */
function rowFor(name: string): HTMLElement {
  return screen.getByRole("heading", { name }).closest("div.group")!.parentElement as HTMLElement
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("skills")
})

beforeEach(() => {
  Object.values(actions).forEach((fn) => typeof fn === "function" && fn.mockReset?.())
})

afterEach(cleanup)

// The author's decision and the operator's decision are two different fields;
// every pair of them has to land on exactly one chip, or a withdrawn package
// reads as still on sale.
describe("SkillGroupRow listing chip", () => {
  it("shows one chip per state the author can be in", () => {
    mount([
      group({ id: "queued", listing: "pending" }),
      group({ id: "live", listing: "listed" }),
      group({ id: "refused", listing: "rejected", listingNote: "含有密钥" }),
      group({ id: "pulled", listing: "delisted", listingNote: "与官方技能重复" }),
      group({ id: "mine-again", publicationStatus: "withdrawn", listing: "listed" }),
    ])

    expect(within(rowFor("queued")).getByText("待审核")).toBeTruthy()
    expect(within(rowFor("live")).getByText("已上架")).toBeTruthy()
    expect(within(rowFor("refused")).getByText("已驳回")).toBeTruthy()
    expect(within(rowFor("pulled")).getByText("已下架")).toBeTruthy()
    expect(within(rowFor("mine-again")).getByText("已撤回")).toBeTruthy()
  })

  it("keeps saying 未上传 for a draft nobody has been asked to look at", () => {
    mount([group({ id: "draft", publicationStatus: "unpublished", listing: null })])
    expect(screen.getByText("未上传")).toBeTruthy()
    expect(screen.queryByText("已上架")).toBeNull()
  })

  it("treats a published row with no listing as listed, for an older backend", () => {
    mount([group({ id: "legacy", listing: null })])
    expect(screen.getByText("已上架")).toBeTruthy()
  })
})

describe("SkillGroupRow reason", () => {
  it("puts a refusal on its own readable line, not only in a tooltip", () => {
    mount([group({ id: "refused", listing: "rejected", listingNote: "SKILL.md 里含有密钥" })])
    expect(screen.queryByText("原因：SKILL.md 里含有密钥")).toBeNull()

    fireEvent.click(screen.getByRole("button", { name: "查看原因" }))
    expect(screen.getByText("原因：SKILL.md 里含有密钥")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "收起原因" }))
    expect(screen.queryByText("原因：SKILL.md 里含有密钥")).toBeNull()
  })

  it("offers no reason line for a state that has nothing to explain", () => {
    mount([group({ id: "live", listing: "listed", listingNote: "stale" })])
    expect(screen.queryByRole("button", { name: "查看原因" })).toBeNull()
  })
})

describe("SkillGroupRow actions", () => {
  it("labels the publish button by what the click actually does", () => {
    mount([
      group({ id: "draft", publicationStatus: "unpublished", listing: null }),
      group({ id: "live", listing: "listed" }),
      group({ id: "refused", listing: "rejected" }),
      group({ id: "pulled", listing: "delisted" }),
    ])
    expect(within(rowFor("draft")).getByRole("button", { name: "上传到商店" })).toBeTruthy()
    expect(within(rowFor("live")).getByRole("button", { name: "更新商店版本" })).toBeTruthy()
    expect(within(rowFor("refused")).getByRole("button", { name: "重新提交" })).toBeTruthy()
    expect(within(rowFor("pulled")).getByRole("button", { name: "重新提交" })).toBeTruthy()
  })

  it("offers withdrawal only while the store still holds a release", () => {
    mount([
      group({ id: "live", listing: "listed" }),
      group({ id: "gone", publicationStatus: "withdrawn", listing: "listed" }),
      group({ id: "draft", publicationStatus: "unpublished", listing: null }),
    ])
    fireEvent.click(within(rowFor("live")).getByRole("button", { name: "撤回发布" }))
    expect(actions.withdrawSkill).toHaveBeenCalledWith(expect.objectContaining({ id: "live" }))
    expect(within(rowFor("gone")).queryByRole("button", { name: "撤回发布" })).toBeNull()
    expect(within(rowFor("draft")).queryByRole("button", { name: "撤回发布" })).toBeNull()
  })

  it("leaves an installed store copy alone — it is somebody else's release", () => {
    mount([group({ id: "theirs", category: "store", publicationStatus: null, listing: null })])
    expect(screen.queryByRole("button", { name: "撤回发布" })).toBeNull()
    expect(screen.getByText("商店安装")).toBeTruthy()
  })
})
