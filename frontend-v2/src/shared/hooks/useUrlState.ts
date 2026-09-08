// List filters and pagination belong in the URL (§8.4): a refresh, a shared
// link and the back button must all land on the same slice of the same list.
// `features/resources` has a hand-written version of this shaped around its own
// filter enum; this one is the generic string-keyed form the admin lists use.
import { useCallback, useMemo } from "react"
import { useSearchParams } from "react-router"

/** The pagination key that survives a filter change is the one that must not. */
const OFFSET_KEY = "offset"

export type UrlStatePatch<K extends string> = Partial<Record<K, string | undefined>>

export interface UrlStateOptions<K extends string> {
  /**
   * Keys that address something *inside* the list — the row that is open, say —
   * rather than narrowing which rows it holds. Changing one of these leaves the
   * page cursor alone, because the reader is still looking at the same list.
   */
  keepPage?: readonly K[]
}

/**
 * Reads `defaults`' keys out of the query string, falling back to the default.
 *
 * `setValues(patch)` merges: a value equal to its default (or `undefined`) is
 * dropped from the URL rather than written out, so an untouched list has a
 * clean address. Changing a filter sends the reader back to the first page —
 * page 4 of the previous filter is a different list — while `offset` itself and
 * anything named in `keepPage` leave the cursor where it was.
 *
 * Updates replace the history entry: stepping back through every keystroke of a
 * search box is not what the back button is for.
 */
export function useUrlState<K extends string>(
  defaults: Readonly<Record<K, string>>,
  // `NoInfer` so the key union comes from `defaults` alone: otherwise a
  // `keepPage: ["id"]` narrows K to "id" and every other key in the bag becomes
  // a type error at the call site.
  options: UrlStateOptions<NoInfer<K>> = {},
): [Record<K, string>, (patch: UrlStatePatch<K>) => void] {
  const [params, setParams] = useSearchParams()

  // Callers pass an object literal, so its identity changes every render while
  // its contents almost never do. Serialising it gives the memos below a
  // dependency that only changes when the defaults actually do.
  const signature = JSON.stringify(defaults)
  const pinned = JSON.stringify(options.keepPage ?? [])

  const values = useMemo(() => {
    const fallbacks = JSON.parse(signature) as Record<K, string>
    const next = {} as Record<K, string>
    for (const key of Object.keys(fallbacks) as K[]) next[key] = params.get(key) ?? fallbacks[key]
    return next
  }, [params, signature])

  const setValues = useCallback(
    (patch: UrlStatePatch<K>) => {
      const fallbacks = JSON.parse(signature) as Record<K, string>
      const keepPage = new Set(JSON.parse(pinned) as K[])
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          let filterChanged = false
          for (const key of Object.keys(patch) as K[]) {
            const value = patch[key] ?? fallbacks[key]
            const current = previous.get(key) ?? fallbacks[key]
            if (key !== OFFSET_KEY && !keepPage.has(key) && value !== current) filterChanged = true
            if (value === fallbacks[key]) next.delete(key)
            else next.set(key, value)
          }
          // Only touch offset if this list actually paginates — deleting a key
          // we do not own would clobber someone else's query string.
          if (filterChanged && OFFSET_KEY in fallbacks) next.delete(OFFSET_KEY)
          return next
        },
        { replace: true },
      )
    },
    [setParams, signature, pinned],
  )

  return [values, setValues]
}
