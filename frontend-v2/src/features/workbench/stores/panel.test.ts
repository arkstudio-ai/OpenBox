// openKind("desktop", { desktopControl }) — the panel opens on the desktop tab
// and, when asked, leaves a request the desktop tab can act on.
import { beforeEach, describe, expect, it } from "vitest"
import { usePanelStore } from "./panel"

beforeEach(() => {
  usePanelStore.setState({ open: false, tabs: [], activeTabId: null, seq: 1, desktopControlRequest: 0 })
})

describe("opening the desktop from a takeover card", () => {
  it("opens a desktop tab and files a control request", () => {
    usePanelStore.getState().openKind("desktop", { desktopControl: true })
    const s = usePanelStore.getState()
    expect(s.open).toBe(true)
    expect(s.tabs.map((t) => t.kind)).toEqual(["desktop"])
    expect(s.activeTabId).toBe(s.tabs[0].id)
    expect(s.desktopControlRequest).toBe(1)
  })

  it("files a new request each time, even on an already open desktop tab", () => {
    usePanelStore.getState().openKind("desktop", { desktopControl: true })
    usePanelStore.getState().openKind("desktop", { desktopControl: true })
    const s = usePanelStore.getState()
    expect(s.tabs).toHaveLength(1)
    expect(s.desktopControlRequest).toBe(2)
  })

  it("leaves the request alone when control was not asked for", () => {
    usePanelStore.getState().openKind("desktop")
    usePanelStore.getState().openKind("review")
    expect(usePanelStore.getState().desktopControlRequest).toBe(0)
    expect(usePanelStore.getState().tabs.map((t) => t.kind)).toEqual(["desktop", "review"])
  })
})
