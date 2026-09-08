import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import { WithdrawSkillDialog } from "./WithdrawSkillDialog"

const target = {
  id: "/data/skills/web-research",
  name: "web-research",
  members: [],
  isPack: false,
  removable: true,
  origin: "container",
  category: "personal",
  publicationStatus: "published",
  listing: "listed",
  isOfficial: false,
} as unknown as SkillGroup

/**
 * The colour names the theme actually defines.
 *
 * `tokens.css` opens its `@theme inline` block with `--color-*: initial`, which
 * clears Tailwind's built-in palette — `black` and `white` included. A utility
 * naming a colour outside this set compiles to no CSS at all, silently, which
 * is exactly how a scrim ends up dimming nothing.
 */
// `new URL(…, import.meta.url)` is not an option: Vite rewrites that pattern
// into an asset URL, which is not a path this can read.
const TOKENS_CSS = resolve(dirname(fileURLToPath(import.meta.url)), "../../../styles/tokens.css")
const TOKENS = new Set(
  [...readFileSync(TOKENS_CSS, "utf8").matchAll(/--color-([a-z0-9-]+):/g)].map((match) => match[1]),
)

function mount() {
  return render(
    <I18nextProvider i18n={i18n}>
      <WithdrawSkillDialog target={target} busy={false} error={null} onCancel={vi.fn()} onConfirm={vi.fn()} />
    </I18nextProvider>,
  )
}

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("skills")
})

afterEach(cleanup)

describe("WithdrawSkillDialog", () => {
  it("dims the page with a colour the theme defines", () => {
    mount()
    const scrim = screen.getByRole("dialog")
    const colours = scrim.className
      .split(/\s+/)
      .filter((name) => name.startsWith("bg-"))
      .map((name) => name.slice(3).split("/")[0])

    expect(colours.length).toBeGreaterThan(0)
    for (const colour of colours) expect([...TOKENS]).toContain(colour)
  })

  it("promises the release is kept and other people's copies are left alone", () => {
    mount()
    expect(screen.getByText("确认把「web-research」从技能商店撤回？")).toBeDefined()
    expect(screen.getByRole("button", { name: "确认撤回" })).toBeDefined()
  })
})
