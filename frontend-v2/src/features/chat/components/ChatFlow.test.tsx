import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { MessageWithParts } from "@/shared/types/api"
import type { Turn } from "../lib/turn-view"
import { ChatFlow } from "./ChatFlow"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
// The rows are not under test here; their own suites cover them.
vi.mock("./UserBubble", () => ({
  UserBubble: ({ message }: { message: MessageWithParts }) => <p>{message.id}</p>,
}))
vi.mock("./AssistantTurn", () => ({ AssistantTurn: () => <p>assistant</p>, TypingRow: () => null }))

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function userTurn(id: string): Turn {
  const message: MessageWithParts = {
    id,
    session_id: "s1",
    role: "user",
    created_at: id,
    parts: [{ id: `${id}-text`, type: "text", text: id }],
  }
  return { kind: "user", key: id, message }
}

describe("ChatFlow older turns", () => {
  // jsdom lays nothing out, so every column counts as too short to scroll —
  // the case where older turns must load without the reader scrolling at all.
  it("asks for older turns while the column cannot scroll, once per top turn", () => {
    const onLoadOlder = vi.fn()
    const props = { sessionId: "s1", busy: false, hasMore: true, onLoadOlder }

    const { rerender } = render(<ChatFlow {...props} turns={[userTurn("m3")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(1)

    // A live turn arriving at the bottom is not a reason to ask again.
    rerender(<ChatFlow {...props} turns={[userTurn("m3"), userTurn("m4")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(1)

    // Once the older page lands there is a new top, and it may still be short.
    rerender(<ChatFlow {...props} turns={[userTurn("m1"), userTurn("m3"), userTurn("m4")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(2)
  })

  it("leaves the top alone when nothing older exists or a page is already loading", () => {
    const onLoadOlder = vi.fn()
    const turns = [userTurn("m1")]

    const { rerender } = render(
      <ChatFlow sessionId="s1" busy={false} turns={turns} hasMore={false} onLoadOlder={onLoadOlder} />,
    )
    rerender(<ChatFlow sessionId="s1" busy={false} turns={turns} hasMore loadingOlder onLoadOlder={onLoadOlder} />)

    expect(onLoadOlder).not.toHaveBeenCalled()
    expect(screen.getByRole("status", { name: "loadingOlder" })).toBeTruthy()
  })
})
