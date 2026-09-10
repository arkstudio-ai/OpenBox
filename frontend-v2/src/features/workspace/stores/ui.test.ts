import { beforeEach, describe, expect, it } from "vitest"
import { useWorkspaceUi } from "./ui"

const KEY = "bossip:workspace-ui"

beforeEach(() => {
  localStorage.clear()
  useWorkspaceUi.setState({ lastSessionId: null })
})

describe("lastSessionId", () => {
  it("remembers the open conversation and persists it", () => {
    useWorkspaceUi.getState().setLastSession("s1")
    expect(useWorkspaceUi.getState().lastSessionId).toBe("s1")
    expect(JSON.parse(localStorage.getItem(KEY)!).lastSessionId).toBe("s1")
  })

  it("does not rewrite storage for the same session", () => {
    useWorkspaceUi.getState().setLastSession("s1")
    localStorage.removeItem(KEY)
    useWorkspaceUi.getState().setLastSession("s1")
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})
