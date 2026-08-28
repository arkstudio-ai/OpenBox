import type { ToolPart } from "@/shared/types/api"
import type { VideoIdentity, VideoMaterialAsset } from "../api/video-identities"

type UnknownRecord = Record<string, unknown>

export interface IdentityCardData {
  identity: VideoIdentity
  material?: VideoMaterialAsset
}

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function parseIdentity(value: unknown): VideoIdentity | null {
  const item = record(value)
  const status = text(item?.status)
  const id = text(item?.identity_id)
  if (!item || !id || !["awaiting_user", "active", "expired", "failed"].includes(status)) return null
  const expiresAt = text(item.expires_at) || null
  const expired =
    status === "awaiting_user" &&
    expiresAt !== null &&
    !Number.isNaN(new Date(expiresAt).getTime()) &&
    new Date(expiresAt).getTime() <= Date.now()
  return {
    identity_id: id,
    label: text(item.label) || "真人主持人",
    provider: text(item.provider),
    group_type: "LivenessFace",
    status: (expired ? "expired" : status) as VideoIdentity["status"],
    provider_group_id: text(item.provider_group_id) || null,
    authorization_url: expired ? null : text(item.authorization_url) || null,
    qr_code: expired ? null : text(item.qr_code) || null,
    expires_at: expiresAt,
    authorized_at: text(item.authorized_at) || null,
    created_at: text(item.created_at) || null,
    updated_at: text(item.updated_at) || null,
    error: text(item.error) || null,
  }
}

function parseMaterial(value: unknown): VideoMaterialAsset | undefined {
  const item = record(value)
  const id = text(item?.material_asset_id)
  const status = text(item?.status)
  if (!item || !id || !["processing", "active", "failed"].includes(status)) return undefined
  return {
    material_asset_id: id,
    identity_id: text(item.identity_id),
    source_asset_id: text(item.source_asset_id),
    provider_asset_id: text(item.provider_asset_id) || null,
    provider_uri: text(item.provider_uri) || null,
    asset_type: text(item.asset_type) === "Video" ? "Video" : "Image",
    status: status as VideoMaterialAsset["status"],
    error: text(item.error) || null,
  }
}

export function cardsFromTools(tools: ToolPart[]): IdentityCardData[] {
  const byId = new Map<string, IdentityCardData>()
  for (const tool of tools) {
    if (tool.tool !== "video_identity") continue
    const metadata = record(tool.metadata)
    const direct = parseIdentity(metadata?.identity)
    if (direct) {
      const previous = byId.get(direct.identity_id)
      byId.set(direct.identity_id, {
        identity: direct,
        material: parseMaterial(metadata?.material_asset) ?? previous?.material,
      })
    }
    if (Array.isArray(metadata?.identities)) {
      for (const raw of metadata.identities) {
        const identity = parseIdentity(raw)
        if (identity) byId.set(identity.identity_id, { identity })
      }
    }
  }
  return [...byId.values()]
}
