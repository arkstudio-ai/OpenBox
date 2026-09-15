// HTTP transport: auth header injection, one 401-refresh-retry, normalized
// errors (ApiError with a stable code for the i18n error map, §10.7).
import { env } from "@/shared/config/env"
import { refreshAccessToken, useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  /** Numbers a quota refusal carries, so the copy can say how far over it is. */
  readonly meta: Record<string, unknown>
  /** The refusal's Retry-After header (delay-seconds or an HTTP date); null when it sent none. */
  retryAfter: string | null = null

  constructor(status: number, code: string, message: string, meta: Record<string, unknown> = {}) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.code = code
    this.meta = meta
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let detail = res.statusText
  let code = `HTTP_${res.status}`
  let meta: Record<string, unknown> = {}
  try {
    const body = (await res.json()) as { detail?: unknown; code?: unknown }
    if (typeof body.code === "string") code = body.code
    if (typeof body.detail === "string") {
      detail = body.detail
    } else if (body.detail && typeof body.detail === "object") {
      // FastAPI puts a structured refusal under `detail`. Quota replies use it
      // to carry a code and the two numbers; reading only the top level left
      // every quota looking like a bare HTTP_429.
      const inner = body.detail as Record<string, unknown>
      if (typeof inner.code === "string") code = inner.code
      if (typeof inner.message === "string") detail = inner.message
      else detail = JSON.stringify(body.detail)
      meta = inner
    } else if (body.detail) {
      detail = JSON.stringify(body.detail)
    }
  } catch {
    // non-JSON body — keep statusText
  }
  const error = new ApiError(res.status, code, detail, meta)
  error.retryAfter = res.headers.get("Retry-After")
  return error
}

async function doFetch(path: string, options: RequestInit, token: string | null): Promise<Response> {
  // A FormData body has to set its own Content-Type, because only the browser
  // knows the multipart boundary it generated. Forcing application/json here
  // left the server with a body it could not parse — an upload came back 422
  // with the file never seen.
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData
  const headers = new Headers(options.headers)
  if (!isFormData && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  if (token) headers.set("Authorization", `Bearer ${token}`)
  return fetch(`${env.apiBase}${path}`, { ...options, headers, credentials: "include" })
}

function scopeRequest(options: RequestInit): RequestInit {
  const headers = new Headers(options.headers)
  const workspaceId = useWorkspaceStore.getState().currentId
  if (workspaceId && !headers.has("X-Workspace-Id")) headers.set("X-Workspace-Id", workspaceId)
  return { ...options, headers }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const { accessToken: token, user } = useAuthStore.getState()
  // Keep the original workspace through authentication refresh. The user may
  // switch workspaces while the first request or refresh is in flight.
  options = scopeRequest(options)
  let res = await doFetch(path, options, token)

  if (res.status === 401 && token) {
    const newToken = await refreshAccessToken()
    if (newToken && user?.id === useAuthStore.getState().user?.id)
      res = await doFetch(path, options, newToken)
  }

  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export interface BlobResponse {
  blob: Blob
  /** Suggested download filename parsed from Content-Disposition, when present. */
  filename: string | null
}

function dispositionFilename(value: string | null): string | null {
  if (!value) return null
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(value)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded)
    } catch {
      return encoded
    }
  }
  return /filename="([^"]+)"/i.exec(value)?.[1] ?? /filename=([^;]+)/i.exec(value)?.[1]?.trim() ?? null
}

/** Authenticated binary download with the same refresh-once behaviour as JSON requests. */
export async function requestBlob(path: string, options: RequestInit = {}): Promise<BlobResponse> {
  const { accessToken: token, user } = useAuthStore.getState()
  options = scopeRequest(options)
  let res = await doFetch(path, options, token)

  if (res.status === 401 && token) {
    const newToken = await refreshAccessToken()
    if (newToken && user?.id === useAuthStore.getState().user?.id)
      res = await doFetch(path, options, newToken)
  }

  if (!res.ok) throw await toApiError(res)
  return {
    blob: await res.blob(),
    filename: dispositionFilename(res.headers.get("content-disposition")),
  }
}

export const http = {
  get: <T>(path: string, options?: RequestInit) => request<T>(path, options),
  post: <T>(path: string, body?: unknown, options?: RequestInit) =>
    request<T>(path, {
      ...options,
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
}
