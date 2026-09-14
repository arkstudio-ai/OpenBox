import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { inspectorEnv, makeRecord, withInspector } from "../../testing/harness"
import { ToolArgumentsPanel } from "./ToolArgumentsPanel"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

describe("ToolArgumentsPanel", () => {
  it("keeps streaming arguments as the raw string and never shows them as an empty object", () => {
    const tool = makeRecord({
      record_id: "tool:call_1",
      kind: "tool",
      call_id: "call_1",
      status: "pending",
      end_seq: null,
      started_at: null,
      finished_at: null,
      duration_ms: null,
      data: { tool: "write", arguments_raw: '{"path": "notes.md", "content": "par' },
    })
    const { container } = render(withInspector(<ToolArgumentsPanel record={tool} />, inspectorEnv([tool])))
    expect(screen.getByText("tool.argumentsGenerating")).toBeTruthy()
    expect(screen.getByText('{"path": "notes.md", "content": "par')).toBeTruthy()
    expect(container.querySelectorAll("[data-availability=pending]").length).toBe(2)
    expect(container.textContent).not.toContain("{}")
  })

  it("marks unparseable arguments and a call that never reached the executor", () => {
    const tool = makeRecord({
      record_id: "tool:bad_call",
      kind: "tool",
      call_id: "bad_call",
      status: "failed",
      started_at: null,
      duration_ms: null,
      data: { tool: "read", arguments_raw: "{", requested_arguments: null, reason: "invalid_arguments" },
    })
    render(withInspector(<ToolArgumentsPanel record={tool} />, inspectorEnv([tool])))
    expect(screen.getByText("tool.argumentsUnparsed")).toBeTruthy()
    expect(screen.getByText("tool.notExecutedArguments")).toBeTruthy()
  })

  it("shows how approval revised the executed arguments and links the decision", () => {
    const tool = makeRecord({
      record_id: "tool:call_2",
      kind: "tool",
      call_id: "call_2",
      data: {
        tool: "bash",
        requested_arguments: { command: "rm -rf build" },
        effective_arguments: { command: "rm -rf build/cache" },
      },
    })
    const permission = makeRecord({
      record_id: "permission:perm_2",
      kind: "permission",
      call_id: "call_2",
      title: "Allow bash",
    })
    render(withInspector(<ToolArgumentsPanel record={tool} />, inspectorEnv([tool, permission])))
    const diff = screen.getByTestId("trajectory-diff")
    expect(diff.textContent).toContain('-   "command": "rm -rf build"')
    expect(diff.textContent).toContain('+   "command": "rm -rf build/cache"')
    expect(screen.getByTestId("trajectory-record-link").textContent).toContain("Allow bash")
  })
})
