export interface PreviewAccessResponse {
  url: string
  mode: "sandboxed_same_origin" | "isolated_origin"
}

export interface AuthorizedPreview {
  url: string
  isolated: boolean
}

export type PreviewAccessRequest = (
  containerId: string,
  port: number,
) => Promise<PreviewAccessResponse>

function assertCleanPreviewUrl(
  response: PreviewAccessResponse,
  containerId: string,
  port: number,
  controlOrigin = typeof window === "undefined" ? "http://openbox.local" : window.location.origin,
): AuthorizedPreview {
  const { url, mode } = response
  if (!url) {
    throw new Error("Preview access response did not include a URL")
  }

  const contractOrigin = "http://openbox.local"
  const parsed = new URL(url, contractOrigin)
  const expectedPath = `/api/containers/${containerId}/preview/${port}/`
  if (parsed.pathname !== expectedPath || parsed.search !== "" || parsed.hash !== "") {
    throw new Error("Preview access response included a non-contract URL")
  }

  if (mode === "sandboxed_same_origin") {
    if (!url.startsWith("/") || url.startsWith("//") || parsed.origin !== contractOrigin) {
      throw new Error("Preview access response included a non-contract URL")
    }
    return { url, isolated: false }
  }

  if (
    mode !== "isolated_origin"
    || !url.startsWith("https://")
    || parsed.protocol !== "https:"
    || parsed.origin === new URL(controlOrigin).origin
  ) {
    throw new Error("Preview access response included a non-contract URL")
  }
  return { url: parsed.href, isolated: true }
}

/**
 * Obtain the scoped HttpOnly preview cookie before exposing the preview URL to
 * an iframe or browser window.
 */
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
