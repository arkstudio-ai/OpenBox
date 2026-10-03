import { afterEach, describe, expect, it, vi } from "vitest"
import { confirmPendingSend, pendingSendIdentity } from "./pending-send"

afterEach(() => vi.restoreAllMocks())

describe("pending send identity", () => {
  it("survives a module reload without persisting the message or attachment names", async () => {
    const save = vi.spyOn(Storage.prototype, "setItem")
    const body = { text: "Private original request", attachments: ["secret-asset"] }
    const first = await pendingSendIdentity("scope-reload", body)
    vi.resetModules()
    const reloaded = await import("./pending-send")
    expect((await reloaded.pendingSendIdentity("scope-reload", body)).id).toBe(first.id)
    expect(JSON.stringify(save.mock.calls)).not.toContain(body.text)
    expect(JSON.stringify(save.mock.calls)).not.toContain("secret-asset")
    first.confirmed()
  })

  it("distinguishes actors, ordered attachments and model options", async () => {
    const body = { text: "Same request", attachments: ["a", "b"], model: "m1", variant: "high" }
    const original = await pendingSendIdentity("user1:workspace1:main", body)
    expect((await pendingSendIdentity("user1:workspace1:main", body)).id).toBe(original.id)
    for (const [scope, next] of [
      ["user2:workspace1:main", body], ["user1:workspace2:main", body],
      ["user1:workspace1:main", { ...body, attachments: ["b", "a"] }],
      ["user1:workspace1:main", { ...body, variant: "low" }],
    ] as const) expect((await pendingSendIdentity(scope, { ...next, attachments: [...next.attachments] })).id).not.toBe(original.id)
    original.confirmed()
  })

  it("accepts a durable message after reload, and an old HTTP receipt cannot erase a new request", async () => {
    const body = { text: "Repeat this after it has visibly succeeded" }
    const old = await pendingSendIdentity("visible-receipt", body)
    vi.resetModules()
    const reloaded = await import("./pending-send")
    reloaded.confirmPendingSend(old.id)
    confirmPendingSend(old.id)
    const next = await reloaded.pendingSendIdentity("visible-receipt", body)
    expect(next.id).not.toBe(old.id)
    old.confirmed()
    vi.resetModules()
    const secondReload = await import("./pending-send")
    expect((await secondReload.pendingSendIdentity("visible-receipt", body)).id).toBe(next.id)
    next.confirmed()
  })
})
