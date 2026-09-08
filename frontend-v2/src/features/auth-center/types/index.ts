// 授权中心 contracts — mirrors backend/platforms/service.py `to_public`.

export type PlatformCapability = "login" | "publish"

export interface Platform {
  key: string
  display: string
  /** "oauth": open-platform grant; "desktop": a site the person logs into on the cloud desktop. */
  kind?: "oauth" | "desktop"
  capabilities?: PlatformCapability[]
  /** False when the deployment has no client key for it; the card is shown greyed. */
  configured?: boolean
  maxGrantDays?: number | null
  /** desktop sites */
  group?: string
  loginUrl?: string
  sensitive?: boolean
  inactivityTtlDays?: number
  reconPending?: boolean
}

export type AccountStatus = "bound" | "expired" | "revoked" | "unknown" | "desktop_offline"

/** Redacted probe summary for a desktop login row — names and timestamps only. */
export interface DesktopProbeDetail {
  cookieOk?: boolean | null
  sessionCookies?: Record<string, (number | null)[] | null> | null
  earliestExpiry?: number | null
  reason?: string | null
  via?: string | null
  probe?: { status?: number | null; code?: unknown; error?: string | null } | null
  display?: Record<string, string | number | boolean> | null
  lastLevel2At?: string | null
}

export interface PlatformAccount {
  id: string
  platform: string
  authKind: "oauth" | "desktop_cookie"
  externalId: string
  unionId: string | null
  nickname: string | null
  avatarUrl: string | null
  scopes: string[]
  status: AccountStatus
  accessExpiresAt: string | null
  refreshExpiresAt: string | null
  /** When a new scan will be needed if every automatic renewal succeeds. */
  estimatedExpiresAt: string | null
  renewCount: number
  renewalsLeft: number
  lastRefreshAt: string | null
  lastProbeAt: string | null
  lastOkAt: string | null
  lastError: string | null
  boundAt: string | null
  boundByUserId: string
  /** desktop_cookie rows only */
  desktopId?: string | null
  siteDisplay?: string
  probeDetail?: DesktopProbeDetail
  predictedExpiresAt?: string | null
}

export interface AppNotification {
  id: string
  kind: string
  title: string
  body: string
  readAt: string | null
  createdAt: string | null
}

export interface NotificationPage {
  items: AppNotification[]
  unread: number
}

export type PublishStatus = "pending" | "published" | "failed" | "expired"

export interface PublishJob {
  id: string
  platform: string
  platformAccountId: string | null
  fileAssetId: string
  title: string
  hashtags: string[]
  shareId: string | null
  status: PublishStatus
  itemId: string | null
  videoId: string | null
  fromOpenId: string | null
  error: string | null
  expiresAt: string | null
  publishedAt: string | null
  createdAt: string | null
}

export interface PublishResult {
  job: PublishJob
  /** The snssdk1128:// share schema; rendered as a QR code, never persisted. */
  schema: string
}

/** Subset of the resource-centre item this feature needs to pick a video. */
export interface VideoAsset {
  id: string
  name: string
  mime: string
  size: number
  kind: string
  createdAt: string
}
