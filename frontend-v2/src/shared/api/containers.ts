// Sandbox container client — platform infrastructure shared by the chat
// composer (sandbox gate) and the workbench (terminal / files).
import { http, requestAbsolute } from "@/shared/api/http"
import type { ContainerInfo } from "@/shared/types/api"

export interface SandboxStatus {
  available: boolean
  container_id?: string
  container_name?: string
  status?: string
}

export interface ListeningPort {
  port: number
  pid: number | null
  process: string | null
  command: string | null
}

export interface PreviewAccessResponse {
  url: string
  mode: "sandboxed_same_origin" | "isolated_origin"
}

export interface PreviewConfigResponse {
  mode: "sandboxed_same_origin" | "isolated_origin"
  origin: string | null
}

function isolatedPreviewOrigin(config: PreviewConfigResponse): string {
  if (config.mode !== "isolated_origin" || !config.origin) throw new Error("preview_config_contract")
  const parsed = new URL(config.origin)
  if (parsed.protocol !== "https:" || parsed.pathname !== "/" || parsed.search !== "" || parsed.hash !== "") {
    throw new Error("preview_config_contract")
  }
  return parsed.origin
}

export const containersApi = {
  sandboxStatus: () => http.get<SandboxStatus>("/api/agent/sandbox/status"),
  list: () => http.get<{ containers: ContainerInfo[] }>("/api/containers"),
  create: (name?: string) => http.post<ContainerInfo>("/api/containers", { name }),
  listeningPorts: (containerId: string) =>
    http.get<{ ports: ListeningPort[] }>(`/api/containers/${encodeURIComponent(containerId)}/ports`),
  previewConfig: () => http.get<PreviewConfigResponse>("/api/preview/config"),
  requestPreviewAccess: async (containerId: string, port: number) => {
    const config = await http.get<PreviewConfigResponse>("/api/preview/config")
    const path = `/api/containers/${encodeURIComponent(containerId)}/preview-token?port=${port}`
    const response =
      config.mode === "isolated_origin"
        ? await requestAbsolute<PreviewAccessResponse>(`${isolatedPreviewOrigin(config)}${path}`, {
            method: "POST",
          })
        : await http.post<PreviewAccessResponse>(path)
    if (response.mode !== config.mode) throw new Error("preview_config_contract")
    return response
  },
  listFiles: (containerId: string, path: string) =>
    http.post<{ entries: FileListEntry[] } | FileListEntry[]>(`/api/containers/${containerId}/files/list`, {
      path,
    }),
}

export interface FileListEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
}

export const containerKeys = {
  all: (userId: string) => ["containers", userId] as const,
  sandbox: (userId: string) => ["sandbox-status", userId] as const,
  ports: (userId: string, containerId: string) => ["container-ports", userId, containerId] as const,
  files: (userId: string, containerId: string, path: string) =>
    ["container-files", userId, containerId, path] as const,
}
