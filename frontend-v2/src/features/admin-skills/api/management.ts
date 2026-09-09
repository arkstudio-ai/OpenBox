import { useMutation, useQuery } from "@tanstack/react-query"
import { http, request } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { ArchiveResult } from "@/shared/ui/ArchiveUploadQueue"
import type { Desktop, DesktopScan, Page, StoreDetail, SkillKind } from "../types"
import { useAdminSkillMutation } from "./admin-skills"

const BASE = "/api/admin/skills"
const path = (id: string) => `${BASE}/store/${encodeURIComponent(id)}`

export interface EntryFields {
  title: string
  description: string
  icon: string
  content?: string
  config?: Record<string, unknown>
}

export function useStoreDetail(id: string | null) {
  const user = useAuthStore((s) => s.user?.id ?? "anon")
  return useQuery({
    queryKey: ["admin-skills", user, "detail", id],
    queryFn: () => http.get<StoreDetail>(path(id!)),
    enabled: !!id,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })
}

export function useSaveEntry() {
  return useAdminSkillMutation(
    (data: { id?: string; name: string; kind: SkillKind; revision: number; fields: EntryFields }) =>
      data.id
        ? http.patch(path(data.id), { ...data.fields, expected_revision: data.revision })
        : http.post(`${BASE}/store`, { ...data.fields, name: data.name, kind: data.kind }),
  )
}

export function useUploadEntries() {
  return useAdminSkillMutation(async (files: File[]) => {
    const body = new FormData()
    files.forEach((file) => body.append("files", file))
    return (await request<{ items: ArchiveResult[] }>(`${BASE}/store/upload`, { method: "POST", body })).items
  })
}

export function useDeleteEntries() {
  return useAdminSkillMutation((body: { catalog_ids: string[]; reason: string }) =>
    http.post<{ items: { catalog_id: string; ok: boolean; error?: string }[] }>(
      `${BASE}/store/batch-delete`,
      body,
    ),
  )
}

export function useRestoreEntry() {
  return useAdminSkillMutation((id: string) => http.post(`${path(id)}/restore`))
}

export function useDesktops(q: string, offset: number) {
  const user = useAuthStore((s) => s.user?.id ?? "anon")
  return useQuery({
    queryKey: ["admin-skills", user, "desktops", q, offset],
    queryFn: () =>
      http.get<Page<Desktop>>(
        `${BASE}/desktops?${new URLSearchParams({ q, offset: String(offset), limit: "20" })}`,
      ),
  })
}

/** Manual scans only: no background polling, auto-start or desktop acquisition. */
export function useScanDesktop(desktop: string, user: string) {
  return useMutation({
    mutationFn: () =>
      http.get<DesktopScan>(
        `${BASE}/desktops/${encodeURIComponent(desktop)}/skills?${new URLSearchParams({ user_id: user })}`,
      ),
  })
}

export function useUninstallFromDesktop(desktop: string, user: string) {
  return useAdminSkillMutation((body: { kind: SkillKind; install_dir: string; reason: string }) =>
    http.post(`${BASE}/desktops/${encodeURIComponent(desktop)}/uninstall`, { ...body, user_id: user }),
  )
}
