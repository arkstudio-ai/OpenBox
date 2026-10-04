import { createContext } from "react"

/** Exact originals backing code blocks inside one rendered turn or user bubble. */
export const MessageCopyContext = createContext<{ sessionId: string; messageIds: string[] } | null>(null)
