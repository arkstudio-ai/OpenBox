import { http } from "@/shared/api/http"

export type VideoIdentityStatus = "awaiting_user" | "active" | "expired" | "failed"

export interface VideoIdentity {
  identity_id: string
  label: string
  provider: string
  group_type: "LivenessFace"
  status: VideoIdentityStatus
  provider_group_id?: string | null
  authorization_url?: string | null
  qr_code?: string | null
  expires_at?: string | null
  authorized_at?: string | null
  created_at?: string | null
  updated_at?: string | null
  error?: string | null
}

export interface VideoMaterialAsset {
  material_asset_id: string
  identity_id: string
  source_asset_id: string
  provider_asset_id?: string | null
  provider_uri?: string | null
  asset_type: "Image" | "Video"
  status: "processing" | "active" | "failed"
  error?: string | null
}

export function refreshVideoIdentity(identityId: string) {
  return http.post<VideoIdentity>(`/api/video/identities/${identityId}/refresh`)
}
