// Filters live in the URL as strings (§8.4) and reach the server as a query
// string. Both directions go through here so an "all" pill and an empty search
// box mean the same thing to the backend: send nothing.

/** The sentinel a filter pill uses for "no restriction"; never sent upstream. */
export const ANY = "all"

export type QueryValue = string | number | undefined | null

/**
 * Builds `?a=1&b=2`, dropping blanks and the `all` sentinel. Returns "" when
 * nothing survives, so callers can append it to a bare path unconditionally.
 */
export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    const text = String(value).trim()
    if (text === "" || text === ANY) continue
    search.set(key, text)
  }
  const text = search.toString()
  return text ? `?${text}` : ""
}
