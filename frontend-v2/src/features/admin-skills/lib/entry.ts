// Small facts about a store row that more than one component needs to agree on.
import type { StatusTone } from "@/shared/ui/StatusPill"
import type { SkillListing } from "@/features/admin-skills/types"

/**
 * Community rows are the ones backed by a `user_skills` record, and only they
 * have a submitted archive, an author to notify, and an `is_official` flag to
 * flip. Catalogue entries (`skill:…` / `mcp:…`) only ever move between listed
 * and delisted, so the prefix is the check — not the origin, which an operator
 * can override to "official" on a community row.
 */
export function isCommunityEntry(catalogId: string): boolean {
  return catalogId.startsWith("community:")
}

/** Semantic tone per listing state (§9.2 — never map a status to a colour here). */
export function listingTone(listing: SkillListing): StatusTone {
  switch (listing) {
    case "listed":
      return "ok"
    case "pending":
      return "warn"
    case "rejected":
      return "danger"
    default:
      return "muted"
  }
}
