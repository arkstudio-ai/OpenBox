import { useQuery } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export const MEDIA = "/api/media-templates"
export const ENTRIES = ["video", "footage_edit", "menu", "poster"] as const
export type Entry = (typeof ENTRIES)[number]
export type MediaStatus = "draft" | "pending" | "listed" | "rejected" | "superseded" | "withdrawn"
export interface Media {
  name?: string
  mime?: string
  media_url?: string | null
  poster_url?: string | null
}

export interface Trial {
  session_id: string
  confirmed: boolean
  output_asset_id: string | null
}

export interface Template extends Media {
  id: string
  title: string
  title_translations?: Record<string, string>
  version?: number
  entry_id: Entry
  scene?: string | null
  scene_label?: string
  source: "creator" | "example" | "legacy_video" | "builtin"
  status: MediaStatus
  reason?: string
  recipe?: string
  source_asset_id?: string
  source_session_id?: string
  can_trial?: boolean
  trials?: Trial[]
}

export interface Review extends Template {
  owner_id?: string
  original?: Media
  input?: Record<string, unknown>
  materials?: (Media | null)[]
  trial_result?: Media | null
  decisions?: { decision: string; reviewer_id: string; reason: string; created_at: string }[]
}

interface Categories {
  entries: { id: Entry; label: string; scenes: { id: string; label: string }[] }[]
}

export type Dish = { name: string; price: string }
export interface Production {
  entry_id: Entry
  session_id: string
  revision: number
  title: string
  status: "draft" | "working" | "blocked" | "completed" | "failed" | "review"
  phase?: string
  materials: { id: string; name: string; mime: string }[]
  output?: { id: string; mime: string; name: string }
  shop: string
  script: string
  reference_copy: string
  poster_title?: string
  product_name?: string
  dishes: Dish[]
  template: string
  keep_original: boolean
  perfect_film?: boolean
  output_language?: string
  reference_example_id?: string
  media_template_trial?: boolean
  remake_prepared?: boolean
  builtin_h3?: boolean
}

export function useMediaScope() {
  const user = useAuthStore((state) => state.user?.id)
  const workspace = useWorkspaceStore((state) => state.currentId)
  return ["media-templates", user, workspace] as const
}

export function useCategories() {
  const scope = useMediaScope()
  return useQuery({
    queryKey: [...scope, "categories"],
    queryFn: () => http.get<Categories>(`${MEDIA}/categories`),
  })
}

export function useProduction(sessionId: string | null) {
  const scope = useMediaScope()
  return useQuery({
    enabled: !!sessionId,
    queryKey: [...scope, "production", sessionId],
    queryFn: ({ signal }) => http.get<Production>(`/api/light/productions/${sessionId}`, { signal }),
    refetchInterval: ({ state }) =>
      ["working", "blocked"].includes(state.data?.status ?? "") ? 2500 : false,
    retry: false,
  })
}
