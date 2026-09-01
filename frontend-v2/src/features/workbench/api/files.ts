// Sandbox file listing + content.
// - list: reuses shared `containersApi.listFiles`; the backend returns `{files}`
//   (v1 contract) but the shared type says `{entries}` — normalise defensively
//   here rather than touching the shared file.
// - content: `GET /api/containers/{id}/files/content?path=<enc>` (backend WIP);
//   404/501 surface as an "unsupported" empty state (no retry).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { containerKeys, containersApi } from "@/shared/api/containers"
import { useUserId, workbenchKeys } from "./keys"

export interface FileNode {
  name: string
  is_dir: boolean
  size?: number | null
  modified?: string | null
}

function normalizeFiles(res: unknown): FileNode[] {
  if (Array.isArray(res)) return res as FileNode[]
  const r = (res ?? {}) as { files?: FileNode[]; entries?: FileNode[] }
  return r.files ?? r.entries ?? []
}

export function useFileListQuery(containerId: string | null, path: string) {
  const userId = useUserId()
  return useQuery({
    queryKey: containerKeys.files(userId, containerId ?? "none", path),
    queryFn: async () => normalizeFiles(await containersApi.listFiles(containerId as string, path)),
    enabled: !!containerId,
    // Agent tools mutate the remote project outside React Query. Keep an open
    // file panel live so newly created files appear without collapsing and
    // reopening the tree.
    refetchInterval: 3_000,
    refetchOnWindowFocus: true,
  })
}

export interface FileContent {
  path: string
  content: string
  truncated?: boolean
}

export function useFileContentQuery(containerId: string | null, path: string | null) {
  const userId = useUserId()
  return useQuery({
    queryKey: workbenchKeys.fileContent(userId, containerId ?? "none", path ?? "none"),
    queryFn: () =>
      http.get<FileContent>(
        `/api/containers/${containerId}/files/content?path=${encodeURIComponent(path as string)}`,
      ),
    enabled: !!containerId && !!path,
    retry: false,
  })
}
