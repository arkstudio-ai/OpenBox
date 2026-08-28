import { describe, expect, it } from "vitest"
import type { ToolPart } from "@/shared/types/api"
import { cardsFromTools } from "../lib/video-identity-card"

function tool(metadata: Record<string, unknown>): ToolPart {
  return {
    type: "tool",
    id: `tool-${Math.random()}`,
    tool: "video_identity",
    status: "completed",
    metadata,
  }
}

function identity(status: string, extras: Record<string, unknown> = {}) {
  return {
    identity_id: "identity-1",
    label: "主持人本人",
    provider: "doubao",
    group_type: "LivenessFace",
    status,
    ...extras,
  }
}

describe("cardsFromTools", () => {
  it("keeps the public H5 card fields and never requires a provider polling token", () => {
    const cards = cardsFromTools([
      tool({
        identity: identity("awaiting_user", {
          authorization_url: "https://api.tokenspace.test/real-validate?token=temporary",
          qr_code: "data:image/png;base64,dGVzdA==",
          expires_at: "2999-08-27T12:00:00Z",
        }),
      }),
    ])

    expect(cards).toHaveLength(1)
    expect(cards[0].identity.authorization_url).toContain("real-validate")
    expect(cards[0].identity.qr_code).toMatch(/^data:image\/png/)
    expect(cards[0].identity).not.toHaveProperty("provider_token")
  })

  it("does not render an authorization link from expired persisted metadata", () => {
    const [card] = cardsFromTools([
      tool({
        identity: identity("awaiting_user", {
          authorization_url: "https://api.tokenspace.test/expired",
          qr_code: "data:image/png;base64,dGVzdA==",
          expires_at: "2000-01-01T00:00:00Z",
        }),
      }),
    ])

    expect(card.identity.status).toBe("expired")
    expect(card.identity.authorization_url).toBeNull()
    expect(card.identity.qr_code).toBeNull()
  })

  it("keeps an approved portrait marker when a later status result updates the identity", () => {
    const [card] = cardsFromTools([
      tool({
        identity: identity("active", { updated_at: "2026-08-27T10:00:00Z" }),
        material_asset: {
          material_asset_id: "binding-1",
          identity_id: "identity-1",
          source_asset_id: "asset-portrait",
          provider_asset_id: "asset-provider-portrait",
          provider_uri: "asset://asset-provider-portrait",
          asset_type: "Image",
          status: "active",
        },
      }),
      tool({
        identity: identity("active", { updated_at: "2026-08-27T10:01:00Z" }),
      }),
    ])

    expect(card.identity.updated_at).toBe("2026-08-27T10:01:00Z")
    expect(card.material?.source_asset_id).toBe("asset-portrait")
  })
})
