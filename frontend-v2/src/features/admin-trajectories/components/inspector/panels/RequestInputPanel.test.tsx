import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { inspectorEnv, makeRecord, withInspector } from "../../testing/harness"
import { RequestInputPanel } from "./RequestInputPanel"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

const REQUEST = makeRecord({
  record_id: "request:req_1",
  kind: "request",
  request_id: "req_1",
  data: {
    capture_level: "adapter_input",
    input: {
      model: "fixture/model",
      temperature: 0.2,
      messages: [
        { role: "system", content: "Inspect precisely" },
        { role: "user", content: [{ type: "text", text: "Describe this" }] },
      ],
      tools: [
        {
          name: "read",
          description: "Read a file",
          parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
        },
      ],
      media_inputs: [
        {
          $media: { payload_id: "pl_removed", availability: "deleted", reason: "explicitly_deleted" },
          source_kind: "service_input",
        },
      ],
      vendor_body: { safety: "strict" },
      omitted_fields: ["api_key", "extra_headers"],
    },
  },
})

describe("RequestInputPanel", () => {
  it("shows the dispatched structure, retained media inputs, other fields and the complete raw snapshot", () => {
    render(withInspector(<RequestInputPanel record={REQUEST} />, inspectorEnv([REQUEST])))
    expect(screen.getByText(/^captureLevel\.adapterInput/)).toBeTruthy()
    expect(screen.getAllByTestId("trajectory-message")).toHaveLength(2)
    expect(screen.getAllByText("Read a file").length).toBeGreaterThan(0)
    const media = screen.getByTestId("trajectory-media-inputs")
    expect(within(media).getByTestId("trajectory-media-ref").getAttribute("data-state")).toBe("deleted")
    expect(screen.getByText("request.otherFields")).toBeTruthy()
    expect(screen.getAllByText(/"strict"/)).toHaveLength(2)
    expect(screen.getByTestId("trajectory-omitted-fields").textContent).toContain("api_key, extra_headers")
    const raw = screen.getByTestId("trajectory-request-raw")
    expect(within(raw).getByText("request.rawSnapshot")).toBeTruthy()
    expect(within(raw).getAllByText(/temperature/).length).toBeGreaterThan(0)
  })

  it("says the input was not recorded instead of showing an empty request", () => {
    const bare = makeRecord({
      record_id: "request:req_2",
      kind: "request",
      request_id: "req_2",
      data: { model: "fixture" },
    })
    const { container } = render(withInspector(<RequestInputPanel record={bare} />, inspectorEnv([bare])))
    expect(screen.getByText("request.captureUnknown")).toBeTruthy()
    expect(container.querySelector("[data-availability=not_recorded]")).not.toBeNull()
  })
})
