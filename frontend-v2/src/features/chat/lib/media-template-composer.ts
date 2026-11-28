import { createContext } from "react"

/** Filled only from the owner's professional conversation. */
export const MediaTemplateComposer = createContext<((assetId: string) => void) | null>(null)
