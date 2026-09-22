import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import { useState } from "react"
import i18n from "@/shared/i18n"
import { CORE_TOOLS, READ_TOOLS, emptyAgent } from "../lib/defaults"
import type { AgentSpec } from "../types"
import { AgentForm } from "./AgentForm"

vi.mock("../api/teams", () => ({
  useCatalogSkills: () => ({
    data: [
      {
        name: "video-production",
        description: "Original",
        display_name: { "zh-CN": "视频制作", "en-US": "Video production" },
        display_description: { "zh-CN": "制作口播视频", "en-US": "Create spoken videos" },
        allowed_tools: ["video_generate"],
        requires_mcp: ["docs"],
      },
      {
        name: "shared",
        description: "Shared dependency",
        allowed_tools: ["video_generate"],
        requires_mcp: ["docs"],
      },
    ],
  }),
  useDefinitions: () => ({
    data: {
      pages: [
        {
          tool_presets: {
            research: READ_TOOLS,
            media: [...READ_TOOLS, "video_generate"],
          },
          core_tools: CORE_TOOLS,
          tool_tiers: { video_generate: "T2" },
        },
      ],
    },
  }),
  useModelCapabilities: () => ({ data: { models: [] } }),
  useMcpCapabilities: () => ({
    data: {
      enabled: true,
      available: true,
      services: [{ name: "docs", tools: ["search_docs"], resources: [] }],
    },
  }),
}))
beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("teams")
})

function Form({
  onChange,
  initial = emptyAgent(),
}: {
  onChange: (spec: AgentSpec) => void
  initial?: AgentSpec
}) {
  const [spec, setSpec] = useState(initial)
  return (
    <I18nextProvider i18n={i18n}>
      <AgentForm
        spec={spec}
        models={[]}
        onChange={(next) => {
          setSpec(next)
          onChange(next)
        }}
      />
    </I18nextProvider>
  )
}

it("keeps all core tools checked and locked when old drafts or presets omit them", () => {
  const onChange = vi.fn()
  render(<Form onChange={onChange} initial={{ ...emptyAgent(), tool_allowlist: [] }} />)
  for (const tool of CORE_TOOLS) {
    const checkbox = screen.getByRole("checkbox", { name: tool }) as HTMLInputElement
    expect(checkbox.checked).toBe(true)
    expect(checkbox.disabled).toBe(true)
  }
  fireEvent.click(screen.getByRole("button", { name: i18n.t("teams:preset.research") }))
  expect(onChange.mock.lastCall?.[0].tool_allowlist).toEqual(expect.arrayContaining(CORE_TOOLS))
})

it("selects and locks Skill tools and MCP scopes through preset changes, then unlocks after deselection", () => {
  const onChange = vi.fn()
  render(<Form onChange={onChange} />)
  const skill = screen.getByRole("button", { name: /视频制作.*video-production/ })
  fireEvent.click(skill)
  const tool = screen.getByRole("checkbox", { name: /^video_generate/ }) as HTMLInputElement
  const server = screen.getByRole("checkbox", { name: /^docs/ }) as HTMLInputElement
  expect(tool.checked && tool.disabled).toBe(true)
  expect(server.checked && server.disabled).toBe(true)
  expect(
    (screen.getByLabelText(i18n.t("teams:mcpPatternsFor", { server: "docs" })) as HTMLTextAreaElement)
      .disabled,
  ).toBe(true)
  const mcpTool = screen.getByRole("checkbox", { name: "search_docs", hidden: true }) as HTMLInputElement
  expect(mcpTool.checked && mcpTool.disabled).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: i18n.t("teams:preset.research") }))
  expect(onChange.mock.lastCall?.[0]).toEqual(
    expect.objectContaining({
      tool_allowlist: expect.arrayContaining(["video_generate", ...CORE_TOOLS]),
      mcp_refs: [{ server: "docs", tools: ["*"] }],
    }),
  )
  fireEvent.click(skill)
  expect(tool.disabled).toBe(false)
  expect(server.disabled).toBe(false)
  fireEvent.click(mcpTool)
  expect(onChange.mock.lastCall?.[0].mcp_refs).toEqual([{ server: "docs", tools: [] }])
  fireEvent.click(server)
  expect(onChange.mock.lastCall?.[0].mcp_refs).toEqual([])
})

it("retains the lock while another selected Skill still requires a shared dependency", () => {
  render(<Form onChange={vi.fn()} />)
  const video = screen.getByRole("button", { name: /视频制作.*video-production/ })
  fireEvent.click(video)
  fireEvent.click(screen.getByRole("button", { name: /shared.*Shared dependency/ }))
  fireEvent.click(video)
  expect((screen.getByRole("checkbox", { name: /^video_generate/ }) as HTMLInputElement).disabled).toBe(true)
  expect((screen.getByRole("checkbox", { name: /^docs/ }) as HTMLInputElement).disabled).toBe(true)
})
afterEach(cleanup)

it("searches a Chinese label but saves the original skill reference", () => {
  const onChange = vi.fn()
  render(
    <I18nextProvider i18n={i18n}>
      <AgentForm spec={emptyAgent()} onChange={onChange} models={[]} />
    </I18nextProvider>,
  )
  fireEvent.change(screen.getByPlaceholderText(i18n.t("teams:searchSkills")), { target: { value: "视频" } })
  fireEvent.click(screen.getByRole("button", { name: /视频制作.*video-production.*制作口播视频/ }))
  expect(onChange).toHaveBeenCalledWith(
    expect.objectContaining({ skill_refs: [{ name: "video-production" }] }),
  )
})

it("saves visual appearance choices as stable values and previews the selected appearance", () => {
  const onChange = vi.fn()
  render(<Form onChange={onChange} initial={{ ...emptyAgent(), name: "测试助手" }} />)
  fireEvent.click(screen.getByRole("radio", { name: "代码" }))
  fireEvent.click(screen.getByRole("radio", { name: "紫色" }))
  expect(onChange.mock.lastCall?.[0].display).toEqual({ icon: "code", color: "violet" })
  expect((screen.getByRole("radio", { name: "代码" }) as HTMLInputElement).checked).toBe(true)
  expect((screen.getByRole("radio", { name: "紫色" }) as HTMLInputElement).checked).toBe(true)
  expect(screen.getByRole("img", { name: "测试助手 的外观预览：代码，紫色" })).toBeTruthy()
  expect(onChange.mock.lastCall?.[0].tool_allowlist).toEqual(expect.arrayContaining(CORE_TOOLS))
})

it.each([
  { display: { icon: "shield-check", color: "sage" }, icon: "审核", color: "绿色" },
  { display: { icon: "🦊", color: "custom" }, icon: "当前图标", color: "当前颜色" },
])(
  "preserves existing appearance values when changing other Agent fields: $display",
  ({ display, icon, color }) => {
    const onChange = vi.fn()
    render(<Form onChange={onChange} initial={{ ...emptyAgent(), display }} />)
    expect((screen.getByRole("radio", { name: icon }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole("radio", { name: color }) as HTMLInputElement).checked).toBe(true)
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText(/^名称/), { target: { value: "改名" } })
    expect(onChange.mock.lastCall?.[0].display).toEqual(display)
    fireEvent.click(screen.getByRole("radio", { name: "红色" }))
    expect(onChange.mock.lastCall?.[0].display).toEqual({ ...display, color: "red" })
  },
)
