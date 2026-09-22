import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { emptyAgent } from "../lib/defaults"
import { AgentForm } from "./AgentForm"

vi.mock("../api/teams", () => ({
  useCatalogSkills: () => ({
    data: [
      {
        name: "video-production",
        description: "Original",
        display_name: { "zh-CN": "视频制作", "en-US": "Video production" },
        display_description: { "zh-CN": "制作口播视频", "en-US": "Create spoken videos" },
      },
    ],
  }),
  useDefinitions: () => ({ data: { pages: [{ tool_presets: {} }] } }),
  useModelCapabilities: () => ({ data: { models: [] } }),
  useMcpCapabilities: () => ({ data: { servers: [] } }),
}))
vi.mock("./McpFields", () => ({ McpFields: () => null }))
beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("teams")
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
