import { describe, expect, it } from "vitest"
import { browserViewWsUrl, terminalWsUrl } from "./ws"

describe("terminalWsUrl", () => {
  it("carries the current session and project as encoded opaque ids", () => {
    const url = new URL(
      terminalWsUrl("desktop/中文", "ticket +/=?", {
        sessionId: "session/你好",
        projectId: "project + one",
      }),
    )

    expect(decodeURIComponent(url.pathname)).toBe("/ws/terminal/desktop/中文")
    expect(url.searchParams.get("ticket")).toBe("ticket +/=?")
    expect(url.searchParams.get("session_id")).toBe("session/你好")
    expect(url.searchParams.get("project_id")).toBe("project + one")
  })

  it("supports a session before project metadata has loaded", () => {
    const url = new URL(terminalWsUrl("desktop", "ticket", { sessionId: "session-1" }))

    expect(url.searchParams.get("session_id")).toBe("session-1")
    expect(url.searchParams.has("project_id")).toBe(false)
  })
})

describe("browserViewWsUrl", () => {
  it("uses the dedicated screenshot stream and safely encodes the ticket", () => {
    const url = new URL(browserViewWsUrl("ticket +/=?"))

    expect(url.pathname).toBe("/ws/browser-view/auto")
    expect(url.searchParams.get("ticket")).toBe("ticket +/=?")
  })
})
