// Query keys carry the user id (ENGINEERING_SPEC §7.2): an admin who signs out
// and back in as someone else must not read the previous session's lists.

/**
 * Prefix for blanket invalidation. Every key below starts with it, so a single
 * `invalidateQueries({ queryKey: adminSkillKeys.root })` after a write refreshes
 * the store, the review queue and the install list at once — a listing change
 * moves all three.
 */
const ROOT = "admin-skills"

export const adminSkillKeys = {
  root: [ROOT] as const,
  all: (userId: string) => [ROOT, userId] as const,
  /** `params` is the serialized query string, so a filter change is a new key. */
  store: (userId: string, params: string) => [ROOT, userId, "store", params] as const,
  review: (userId: string, params: string) => [ROOT, userId, "review", params] as const,
  reviewDetail: (userId: string, catalogId: string) => [ROOT, userId, "review", "detail", catalogId] as const,
  installs: (userId: string, params: string) => [ROOT, userId, "installs", params] as const,
  /**
   * The skill centre's cache root, restated rather than imported: a feature may
   * not import another feature (eslint boundaries), but sharing a key is not an
   * import. Approving or delisting changes what the user-facing store shows.
   */
  skillCenter: (userId: string) => ["skill-center", userId] as const,
}
