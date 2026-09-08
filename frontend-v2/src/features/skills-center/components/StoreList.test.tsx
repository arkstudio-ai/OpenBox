import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { buildStoreShelves } from "@/features/skills-center/lib/store-sections"
import type { CatalogMcp, CatalogSkill, KindFilter } from "@/features/skills-center/types"
import { StoreList } from "./StoreList"

function skill(id: string, extra: Partial<CatalogSkill> = {}): CatalogSkill {
  return {
    id,
    kind: "skill",
    name: id,
    title: id,
    icon: "🔎",
    description: `${id} desc`,
    installed: false,
    requires_mcp: [],
    missing_mcp: [],
    install: {},
    ...extra,
  }
}

function mcp(id: string, extra: Partial<CatalogMcp> = {}): CatalogMcp {
  return {
    id,
    kind: "mcp",
    name: id,
    title: id,
    icon: "🧩",
    description: `${id} desc`,
    installed: false,
    config: { type: "stdio" },
    ...extra,
  }
}

const onInstallSkill = vi.fn()
const onInstallMcp = vi.fn()

function mount(
  catalog: { skills: CatalogSkill[]; mcp: CatalogMcp[] },
  filters: { kind: KindFilter; query: string } = { kind: "all", query: "" },
) {
  return render(
    <I18nextProvider i18n={i18n}>
      <StoreList
        shelves={buildStoreShelves(catalog, filters)}
        onInstallSkill={onInstallSkill}
        onInstallMcp={onInstallMcp}
      />
    </I18nextProvider>,
  )
}

const sectionFor = (heading: string) =>
  screen.getByRole("heading", { name: heading }).parentElement as HTMLElement

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("skills")
})

afterEach(() => {
  onInstallSkill.mockReset()
  onInstallMcp.mockReset()
  cleanup()
})

describe("StoreList", () => {
  it("cuts the store by who published each entry", () => {
    mount({
      skills: [skill("ours", { origin: "official" }), skill("theirs", { origin: "community" })],
      mcp: [mcp("hosted", { origin: "third_party" })],
    })
    expect(within(sectionFor("官方")).getByText("ours")).toBeTruthy()
    expect(within(sectionFor("用户共享")).getByText("theirs")).toBeTruthy()
    expect(within(sectionFor("第三方")).getByText("hosted")).toBeTruthy()
  })

  it("marks a pinned entry and counts installs that happened", () => {
    mount({
      skills: [skill("pinned", { origin: "official", featured: true, installs_count: 12 })],
      mcp: [],
    })
    expect(screen.getByText("置顶")).toBeTruthy()
    expect(screen.getByText("12 次安装")).toBeTruthy()
  })

  it("says nothing about an entry nobody has installed", () => {
    // Zero is not evidence: a fresh entry and an ignored one look identical.
    mount({ skills: [skill("new", { origin: "official", installs_count: 0 })], mcp: [] })
    expect(screen.queryByText("0 次安装")).toBeNull()
  })

  it("says which kind a row is, now that a shelf mixes both", () => {
    mount({
      skills: [skill("a-skill", { origin: "community" })],
      mcp: [mcp("a-server", { origin: "community" })],
    })
    const section = sectionFor("用户共享")
    expect(within(section).getByText("Skill")).toBeTruthy()
    expect(within(section).getByText("MCP")).toBeTruthy()
  })

  it("routes an install to the handler for that kind", () => {
    mount({
      skills: [skill("a-skill", { origin: "community" })],
      mcp: [mcp("a-server", { origin: "third_party" })],
    })
    fireEvent.click(within(sectionFor("用户共享")).getByRole("button", { name: "安装" }))
    expect(onInstallSkill).toHaveBeenCalledWith(expect.objectContaining({ id: "a-skill" }))
    fireEvent.click(within(sectionFor("第三方")).getByRole("button", { name: "安装" }))
    expect(onInstallMcp).toHaveBeenCalledWith(expect.objectContaining({ id: "a-server" }))
  })

  it("distinguishes an empty store from a search that found nothing", () => {
    mount({ skills: [], mcp: [] })
    expect(screen.getByText("商店里还没有条目。")).toBeTruthy()
    cleanup()

    mount({ skills: [skill("ours", { origin: "official" })], mcp: [] }, { kind: "all", query: "zzz" })
    expect(screen.getByText("没有匹配的结果。")).toBeTruthy()
  })
})
