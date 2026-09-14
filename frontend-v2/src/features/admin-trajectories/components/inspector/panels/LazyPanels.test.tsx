// Request input, system prompt, system diff and tool catalog read the `$ref`
// values of the content they show when opened, and then render exactly what a
// fully expanded record renders. Every other panel gets the whole record read.
import { QueryClient } from "@tanstack/react-query"
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react"
import type { ReactElement } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/shared/api/http"
import { trajectoryApi } from "../../../api/endpoints"
import { useTrajectoryAccess } from "../../../stores/access"
import type { RefEnvelope, TrajectoryRecord } from "../../../types/protocol"
import { inspectorEnv, makeRecord, withInspector } from "../../testing/harness"
import { TabPanel } from "../TabPanel"
import { RequestInputPanel } from "./RequestInputPanel"
import { SummaryPanel } from "./SummaryPanel"
import { SystemDiffPanel } from "./SystemDiffPanel"
import { SystemPromptPanel } from "./SystemPromptPanel"
import { ToolCatalogPanel } from "./ToolCatalogPanel"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

vi.mock("../../../api/endpoints", () => ({ trajectoryApi: { blob: vi.fn() } }))

const blob = vi.mocked(trajectoryApi.blob)
const stores = new Map<string, unknown>()

/** Content kept once by digest, as the worker stores it. */
function stored(value: unknown): RefEnvelope {
  const sha256 = (stores.size + 1).toString(16).padStart(64, "0")
  stores.set(sha256, value)
  return {
    $ref: {
      sha256,
      size_bytes: JSON.stringify(value).length,
      media_type: "application/json",
      kind: "value",
      payload_id: `pld_${stores.size}`,
    },
  }
}

const SYSTEM = "Inspect the repository carefully and answer precisely. ".repeat(30)
const TOOLS = [
  {
    name: "read",
    description: "Read a file",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
  },
  {
    name: "write",
    description: "Write a file",
    parameters: { type: "object", properties: { path: { type: "string" }, text: { type: "string" } } },
  },
]
const MESSAGES = [
  { role: "user", content: [{ type: "text", text: "Describe this repository" }] },
  { role: "assistant", content: "A monorepo with a backend and two frontends." },
]
const OPTIONS = { model: "fixture/model", temperature: 0.2, omitted_fields: ["api_key"] }

const SYSTEM_REF = stored(SYSTEM)
const TOOLS_REF = stored(TOOLS)
const MESSAGE_REFS = MESSAGES.map(stored)
const BEFORE_TOOLS_REF = stored(TOOLS.slice(0, 1))
const INPUT_REF = stored({ ...OPTIONS, system: SYSTEM_REF, messages: MESSAGE_REFS, tools: TOOLS_REF })

const request = (input: unknown) =>
  makeRecord({
    record_id: "request:req_1",
    kind: "request",
    request_id: "req_1",
    data: { capture_level: "adapter_input", input },
  })
const REQUEST_FULL = request({ ...OPTIONS, system: SYSTEM, messages: MESSAGES, tools: TOOLS })
const REQUEST_REFS = request({ ...OPTIONS, system: SYSTEM_REF, messages: MESSAGE_REFS, tools: TOOLS_REF })
const REQUEST_STORED_WHOLE = request(INPUT_REF)

const system = (data: Record<string, unknown>) =>
  makeRecord({
    record_id: "system:req_2",
    kind: "system",
    request_id: "req_2",
    data: { capture_level: "adapter_input", source_request_id: "req_2", ...data },
  })
const SYSTEM_FULL = system({
  system: SYSTEM,
  tools: TOOLS,
  before: { system: "Answer briefly.", tools: TOOLS.slice(0, 1) },
})
const SYSTEM_REFS = system({
  system: SYSTEM_REF,
  tools: TOOLS_REF,
  before: { system: "Answer briefly.", tools: BEFORE_TOOLS_REF },
})

const digests = (refs: readonly RefEnvelope[]) => refs.map((envelope) => envelope.$ref.sha256).sort()
const requested = () => [...new Set(blob.mock.calls.map(([, digest]) => digest))].sort()

interface Case {
  name: string
  panel: (record: TrajectoryRecord) => ReactElement
  full: TrajectoryRecord
  refs: TrajectoryRecord
  reads: RefEnvelope[]
}

const CASES: Case[] = [
  {
    name: "request input",
    panel: (record) => <RequestInputPanel record={record} />,
    full: REQUEST_FULL,
    refs: REQUEST_REFS,
    reads: [SYSTEM_REF, TOOLS_REF, ...MESSAGE_REFS],
  },
  {
    name: "request input kept whole",
    panel: (record) => <RequestInputPanel record={record} />,
    full: REQUEST_FULL,
    refs: REQUEST_STORED_WHOLE,
    reads: [INPUT_REF, SYSTEM_REF, TOOLS_REF, ...MESSAGE_REFS],
  },
  {
    name: "system prompt",
    panel: (record) => <SystemPromptPanel record={record} />,
    full: SYSTEM_FULL,
    refs: SYSTEM_REFS,
    reads: [SYSTEM_REF],
  },
  {
    name: "system diff",
    panel: (record) => <SystemDiffPanel record={record} />,
    full: SYSTEM_FULL,
    refs: SYSTEM_REFS,
    reads: [SYSTEM_REF, TOOLS_REF, BEFORE_TOOLS_REF],
  },
  {
    name: "tool catalog",
    panel: (record) => <ToolCatalogPanel record={record} />,
    full: SYSTEM_FULL,
    refs: SYSTEM_REFS,
    reads: [TOOLS_REF],
  },
  {
    name: "summary (any other panel)",
    panel: (record) => <TabPanel record={record} tab="summary" />,
    full: REQUEST_FULL,
    refs: REQUEST_REFS,
    reads: [SYSTEM_REF, TOOLS_REF, ...MESSAGE_REFS],
  },
]

let client: QueryClient

/** Generated ids differ from one render to the next; nothing else may. */
const markup = (container: HTMLElement) => container.innerHTML.replace(/«[^»]*»|:r[0-9a-z]+:/g, "id")

function show(element: ReactElement) {
  return render(withInspector(element, inspectorEnv([], { throughSeq: "10", refs: true }), client))
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  blob.mockImplementation(async (_sid, digest) => stores.get(digest))
})

afterEach(() => {
  cleanup()
  client.clear()
  vi.resetAllMocks()
})

describe("panels reading content references", () => {
  it.each(CASES)(
    "$name renders exactly as the expanded record once its references are read",
    async (entry) => {
      const expanded = show(entry.panel(entry.full))
      const expected = markup(expanded.container)
      expanded.unmount()
      expect(blob).not.toHaveBeenCalled()

      const lazy = show(entry.panel(entry.refs))
      expect(lazy.getByTestId("trajectory-content-loading").textContent).toContain("content.loading")
      await waitFor(() => expect(lazy.queryByTestId("trajectory-content-loading")).toBeNull())
      expect(markup(lazy.container)).toBe(expected)
      // Only the content this panel shows, each digest once, at the shown watermark.
      expect(requested()).toEqual(digests(entry.reads))
      expect(blob).toHaveBeenCalledTimes(entry.reads.length)
      expect(blob.mock.calls.every(([sid, , seq]) => sid === "ses_test" && seq === "10")).toBe(true)
    },
  )

  it("fails visibly and renders the panel once a retry reads the content", async () => {
    blob.mockRejectedValue(new ApiError(500, "HTTP_500", "unavailable"))
    const view = show(<SystemPromptPanel record={SYSTEM_REFS} />)
    await waitFor(() => expect(view.getByTestId("trajectory-content-failed")).toBeTruthy())
    expect(view.getByRole("alert").textContent).toContain("content.failed")
    expect(view.queryByTestId("trajectory-system-prompt")).toBeNull()

    blob.mockImplementation(async (_sid, digest) => stores.get(digest))
    fireEvent.click(view.getByRole("button", { name: "common.retry" }))
    await waitFor(() => expect(view.getByTestId("trajectory-system-prompt")).toBeTruthy())
  })

  it("states deleted content instead of rendering the reference", async () => {
    blob.mockRejectedValue(new ApiError(410, "trajectory_content_deleted", "deleted"))
    const view = show(<ToolCatalogPanel record={SYSTEM_REFS} />)
    await waitFor(() => expect(view.container.querySelector("[data-availability=deleted]")).not.toBeNull())
    expect(view.container.textContent).not.toContain("sha256")
  })
})

describe("without capabilities.refs", () => {
  // Such a server expands every detail, so an object that merely looks like a
  // reference is captured data: each panel renders the record it is given at
  // once, as before, and nothing is read.
  const legacy = (element: ReactElement) =>
    render(withInspector(element, inspectorEnv([], { throughSeq: "10", refs: false }), client))

  it.each(CASES)("$name renders the record as given and reads nothing", (entry) => {
    const view = legacy(entry.panel(entry.refs))
    expect(view.queryByTestId("trajectory-content-loading")).toBeNull()
    expect(view.queryByTestId("trajectory-content-failed")).toBeNull()
    expect(blob).not.toHaveBeenCalled()
    expect(client.getQueryCache().getAll()).toHaveLength(0)
  })

  it("puts nothing between a tab and its panel", () => {
    const direct = legacy(<SummaryPanel record={REQUEST_REFS} />)
    const expected = markup(direct.container)
    direct.unmount()
    const tab = legacy(<TabPanel record={REQUEST_REFS} tab="summary" />)
    expect(markup(tab.container)).toBe(expected)
    expect(blob).not.toHaveBeenCalled()
  })
})
