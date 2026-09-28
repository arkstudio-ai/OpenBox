// Store contracts — mirrors backend/store/service.py `to_public` and
// backend/api/stores.py (docs/OPS_CASE_PLAN.md §3.1).

export type StoreCategory = "food" | "beauty" | "retail" | "other"
export type StorePlatform = "douyin_laike" | "meituan_merchant"
export type PersonaStatus = "none" | "proposed" | "active"

export interface PlatformBinding {
  status: string
  accountId?: string | null
  accountName?: string | null
  role?: string | null
  shopId?: string | null
  boundAt?: string | null
  updatedAt?: string | null
}

export interface Store {
  id: string
  workspaceId: string
  name: string
  category: StoreCategory | string
  /** Whether the deployment serves this category yet. */
  categoryOpen: boolean
  mainPlatforms: string[]
  address?: string | null
  city?: string | null
  platformBindings: Record<string, PlatformBinding>
  dataSources?: Record<string, unknown> | null
  personaStatus: PersonaStatus
  personaSessionId?: string | null
  personaStartedAt?: string | null
  createdAt: string
  updatedAt: string
}

/** `GET /api/stores`: at most one store per workspace, plus the catalogue
 *  the setup form is built from. */
export interface StoreList {
  items: Store[]
  categories: string[]
  openCategories: string[]
  platforms: string[]
}

export interface StoreCreateInput {
  name: string
  category: StoreCategory
  main_platforms: StorePlatform[]
}

export type StoreUpdateInput = Partial<StoreCreateInput> & { address?: string; city?: string }

export interface StarterCard {
  title: string
  hint: string
}

export interface StarterCards {
  items: StarterCard[]
  personaStatus: PersonaStatus
}
