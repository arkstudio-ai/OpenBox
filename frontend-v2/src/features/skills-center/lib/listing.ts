// What the author's own list says about a package they published.
//
// Two independent facts decide one chip: `publication_status` is the author's
// decision (is there a release at all, or did they pull it), `listing` is the
// operator's (is it on the shelf, queued, or refused). Rendering them as two
// badges made a withdrawn-but-approved package read as still on sale, so they
// collapse here into the single state the author actually cares about.
import type { ListingState } from "@/features/skills-center/types"

/** The five states a published package can be in, from the author's side. */
export type ListingChip = "pending" | "listed" | "rejected" | "delisted" | "withdrawn"

export type ListingTone = "muted" | "ok" | "warn" | "danger"

/** Semantic, not decorative: "delisted" is an operator's decision the author
 *  can act on, "rejected" is a refusal — the second one is the louder colour. */
export const LISTING_TONES: Record<ListingChip, ListingTone> = {
  pending: "warn",
  listed: "ok",
  rejected: "danger",
  delisted: "muted",
  withdrawn: "muted",
}

/** The states whose reason (`listing_note`) the author must be able to read. */
const EXPLAINED: ReadonlySet<ListingChip> = new Set<ListingChip>(["rejected", "delisted"])

export function explainsItself(chip: ListingChip): boolean {
  return EXPLAINED.has(chip)
}

/**
 * The chip for one personal package, or null when there is nothing to say.
 *
 * A draft that was never submitted gets no chip: "not uploaded" is already the
 * row's other badge, and a second one repeating it in operator vocabulary is
 * noise. An absent `listing` means an older backend that had no shelf states,
 * where every published package was by definition listed.
 */
export function listingChipFor(
  publicationStatus: "unpublished" | "published" | "withdrawn" | null | undefined,
  listing: ListingState | null | undefined,
): ListingChip | null {
  if (publicationStatus === "withdrawn") return "withdrawn"
  if (publicationStatus !== "published") return null
  if (listing === "pending" || listing === "rejected" || listing === "delisted") return listing
  return "listed"
}

/**
 * Whether publishing again is a re-submission rather than a first upload.
 *
 * A refusal and a delisting both leave a release the operator has already
 * ruled on, and so does a withdrawal — the button that sends it back should
 * not claim to be uploading something new.
 */
export function isResubmission(chip: ListingChip | null): boolean {
  return chip === "rejected" || chip === "delisted" || chip === "withdrawn"
}

/** Whether the author still has a release the store could be showing. */
export function canWithdraw(chip: ListingChip | null): boolean {
  return chip !== null && chip !== "withdrawn"
}
