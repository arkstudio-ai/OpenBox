import { describe, expect, it } from "vitest"
import { standalonePage } from "./standalonePage"

describe("standalonePage", () => {
  it("treats the greeting and sessions as the chat surface", () => {
    expect(standalonePage("/app")).toBeNull()
    expect(standalonePage("/app/s/abc123")).toBeNull()
    // A session id that contains a page name is still a session.
    expect(standalonePage("/app/s/settings-notes")).toBeNull()
  })

  it("matches every centre with and without a sub-route", () => {
    expect(standalonePage("/app/settings")).toBe("settings")
    expect(standalonePage("/app/settings/models")).toBe("settings")
    expect(standalonePage("/app/billing")).toBe("billing")
    expect(standalonePage("/app/billing/usage")).toBe("billing")
    expect(standalonePage("/app/cron")).toBe("cron")
    expect(standalonePage("/app/resources")).toBe("resources")
    expect(standalonePage("/app/auth-center")).toBe("authCenter")
    expect(standalonePage("/app/skills")).toBe("skills")
  })

  it("covers the whole admin console", () => {
    expect(standalonePage("/app/admin")).toBe("admin")
    expect(standalonePage("/app/admin/fleet")).toBe("admin")
    expect(standalonePage("/app/admin/billing/workspaces/ws1")).toBe("admin")
  })
})
