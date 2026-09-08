// The store's sections, recomputed only when the catalogue or the filters move.
import { useMemo } from "react"
import { buildStoreShelves, type StoreShelves } from "@/features/skills-center/lib/store-sections"
import type { Catalog, KindFilter } from "@/features/skills-center/types"

export function useStoreShelves(catalog: Catalog | undefined, kind: KindFilter, query: string): StoreShelves {
  return useMemo(() => buildStoreShelves(catalog, { kind, query }), [catalog, kind, query])
}
