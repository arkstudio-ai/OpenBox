// Query keys carry the user id (ENGINEERING_SPEC §7.2).
import type { ResourceQuery } from "@/features/resources/types"

export const resourceKeys = {
  all: (userId: string) => ["resources", userId] as const,
  list: (userId: string, query: ResourceQuery) =>
    [
      "resources",
      userId,
      query.project,
      query.source,
      query.kind,
      query.q,
      query.sort,
      query.limit ?? 0,
    ] as const,
  projects: (userId: string) => ["projects", userId] as const,
}
