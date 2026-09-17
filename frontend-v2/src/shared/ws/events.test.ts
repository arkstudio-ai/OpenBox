import { describe, expect, it } from "vitest"
import { interactionRequestId } from "./events"

describe("interactionRequestId", () => {
  it("reads the compatibility request_id field", () => {
    expect(interactionRequestId({ id: "new-id", request_id: "compat-id" })).toBe("compat-id")
  })

  it("keeps id-only events from older backend workers working", () => {
    expect(interactionRequestId({ id: "legacy-id" })).toBe("legacy-id")
  })

  it("keeps request_id-only events working", () => {
    expect(interactionRequestId({ request_id: "request-id" })).toBe("request-id")
  })
})
