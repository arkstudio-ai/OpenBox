import type { PreviewAccessResponse } from "@/shared/api/containers"

export type PreviewAccessRequest = (containerId: string, port: number) => Promise<PreviewAccessResponse>

export interface AuthorizedPreview {
  url: string
  isolated: boolean
}

/** Accept only the clean, same-origin path issued by the backend contract. */
export function assertCleanPreviewUrl(
  response: PreviewAccessResponse,
  containerId: string,
  port: number,
  controlOrigin = typeof window === "undefined" ? "http://openbox.local" : window.location.origin,
): AuthorizedPreview {
  const { url, mode } = response
  const contractOrigin = "http://openbox.local"
  const parsed = new URL(url, contractOrigin)
  const expectedPath = `/api/containers/${containerId}/preview/${port}/`

  const cleanPath = parsed.pathname === expectedPath && parsed.search === "" && parsed.hash === ""
  if (!cleanPath) {
    throw new Error("preview_url_contract")
  }

  if (mode === "sandboxed_same_origin") {
    if (!url.startsWith("/") || url.startsWith("//") || parsed.origin !== contractOrigin) {
      throw new Error("preview_url_contract")
    }
    return { url, isolated: false }
  }

  if (
    mode !== "isolated_origin" ||
    !url.startsWith("https://") ||
    parsed.protocol !== "https:" ||
    parsed.origin === new URL(controlOrigin).origin
  ) {
    throw new Error("preview_url_contract")
  }
  return { url: parsed.href, isolated: true }
}

/** Seed the scoped HttpOnly cookie before exposing a preview URL to a frame. */
export async function authorizePreviewNavigation(
  requestAccess: PreviewAccessRequest,
  containerId: string,
  port: number,
  navigate: (preview: AuthorizedPreview) => void,
): Promise<AuthorizedPreview> {
  const preview = assertCleanPreviewUrl(await requestAccess(containerId, port), containerId, port)
  navigate(preview)
  return preview
}
