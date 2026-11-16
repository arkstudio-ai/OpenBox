import { render, screen, fireEvent } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { recordedVoice } from "../lib/voice-message"
import { VoiceMessageBubble } from "./VoiceMessageBubble"
import type { MessageWithParts } from "@/shared/types/api"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("../api/assets", () => ({ useAssetUrl: () => ({ data: { url: "https://media.test/voice.m4a" } }) }))

describe("recorded voice", () => {
  it("uses durable metadata, while a normal text message remains text", () => {
    const message = {
      id: "m1",
      session_id: "s1",
      role: "user",
      created_at: "2026-11-16T00:00:00Z",
      parts: [
        {
          id: "p1",
          type: "text",
          text: "Do not cancel",
          origin_ref: {
            voice_message: { asset_id: "audio-1", duration_ms: 4000 },
          },
        },
      ],
    } as MessageWithParts
    expect(recordedVoice(message)).toEqual({ assetId: "audio-1", durationMs: 4000 })
    expect(recordedVoice({ parts: [{ type: "text", text: "voice" }] } as MessageWithParts)).toBeNull()
  })
  it("offers playback and an expandable transcript", () => {
    render(<VoiceMessageBubble assetId="audio-1" durationMs={4000} transcript="Do not cancel" />)
    expect(screen.getByLabelText("voiceMessage.switchVoice").getAttribute("src")).toBe(
      "https://media.test/voice.m4a",
    )
    fireEvent.click(screen.getByText("voiceMessage.showText"))
    expect(screen.getByText("Do not cancel").closest("details")?.open).toBe(true)
  })
})
