import { createContext, useContext, type ReactNode } from "react"
import type { ContentEnvelope } from "./media"

/**
 * How a retained-content wrapper (`$payload` / `$media`) found inside a JSON
 * value is rendered. The inspector provides the protected viewer; outside it,
 * JSON shows the wrapper as data. Kept in context so the JSON tree does not
 * import the payload viewer that itself renders JSON.
 */
export const EnvelopeRendererContext = createContext<((value: ContentEnvelope) => ReactNode) | null>(null)

export function useEnvelopeRenderer(): ((value: ContentEnvelope) => ReactNode) | null {
  return useContext(EnvelopeRendererContext)
}
