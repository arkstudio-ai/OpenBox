import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { buildTree, flattenTree } from "../../utils/view"
import { makeRecord } from "../testing/harness"
import { buildOrdinals } from "./ordinals"
import { RecordTable } from "./RecordTable"
import type { RowContext } from "./RecordRowView"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

// jsdom has no layout: give the scroll viewport a size so the virtualizer has rows to render.
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    bottom: 600,
    right: 960,
    width: 960,
    height: 600,
    toJSON: () => ({}),
  } as DOMRect)
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600)
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(960)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const records = [
  makeRecord({
    record_id: "request:req_a",
    kind: "request",
    request_id: "req_a",
    start_seq: "3",
    title: "fixture/model",
  }),
  makeRecord({
    record_id: "assistant:req_a",
    kind: "assistant",
    request_id: "req_a",
    start_seq: "4",
    title: "assistant",
    preview: "我来",
  }),
  makeRecord({
    record_id: "tool:call_a",
    kind: "tool",
    request_id: "req_a",
    call_id: "call_a",
    start_seq: "5",
    title: "read",
    result_preview: "one two",
  }),
]

function setup(selectedId: string | null, collapsed: Record<string, true> = {}) {
  const tree = buildTree(records)
  const rows = flattenTree(tree, collapsed)
  const context: RowContext = {
    ordinals: buildOrdinals(tree.nodes.values()),
    clock: null,
    timeMode: "utc",
    locale: "en-US",
    agentNames: new Map(),
  }
  const onSelect = vi.fn()
  const onToggle = vi.fn()
  const view = render(
    <RecordTable
      rows={rows}
      selectedId={selectedId}
      onSelect={onSelect}
      onToggle={onToggle}
      context={context}
    />,
  )
  return { ...view, onSelect, onToggle, rows }
}

describe("RecordTable", () => {
  it("renders one stable row per record, nested by recorded relations", () => {
    setup(null)
    const rows = screen.getAllByTestId("trajectory-record-row")
    expect(rows.map((row) => row.getAttribute("data-record-id"))).toEqual([
      "request:req_a",
      "assistant:req_a",
      "tool:call_a",
    ])
    expect(rows.map((row) => row.getAttribute("aria-level"))).toEqual(["1", "2", "2"])
    expect(rows[0].getAttribute("aria-expanded")).toBe("true")
    expect(rows[2].textContent).toContain("one two")
  })

  it("moves the selection with the keyboard and exposes it as the active descendant", () => {
    const { onSelect } = setup("request:req_a")
    const grid = screen.getByRole("treegrid")
    const active = grid.getAttribute("aria-activedescendant")
    expect(active && document.getElementById(active)?.getAttribute("data-record-id")).toBe("request:req_a")
    expect(document.getElementById(active!)?.getAttribute("aria-selected")).toBe("true")
    fireEvent.keyDown(grid, { key: "ArrowDown" })
    expect(onSelect).toHaveBeenLastCalledWith("assistant:req_a")
    fireEvent.keyDown(grid, { key: "End" })
    expect(onSelect).toHaveBeenLastCalledWith("tool:call_a")
  })

  it("folds a group from the keyboard and from its toggle without selecting it", () => {
    const { onSelect, onToggle } = setup("request:req_a")
    fireEvent.keyDown(screen.getByRole("treegrid"), { key: "ArrowLeft" })
    expect(onToggle).toHaveBeenCalledWith("request:req_a")
    onSelect.mockClear()
    fireEvent.click(screen.getByRole("button", { name: /table.collapse/ }))
    expect(onToggle).toHaveBeenCalledTimes(2)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it("hides collapsed children", () => {
    setup(null, { "request:req_a": true })
    expect(screen.getAllByTestId("trajectory-record-row")).toHaveLength(1)
  })
})
