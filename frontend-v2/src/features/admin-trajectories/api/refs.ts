// Content-addressed JSON (`$ref`, SPEC §7.3 and §11.2). Read with
// `expand=refs`, a record detail keeps a request's system prompt, tool
// catalog, each message and any oversized value as references; a panel reads
// the ones it shows through `/blobs/{sha256}?through_seq=H`. A digest names
// immutable content, so an answer with content is cached for the viewer and
// target by digest and never read again. An answer without content — not
// visible at that watermark, deleted, corrupt — is asked again the next time
// something needs it.
import { queryOptions, type QueryClient } from "@tanstack/react-query"
import { ApiError } from "@/shared/api/http"
import { isRefEnvelope, type RefEnvelope, type Seq } from "../types/protocol"
import { isPlainObject } from "../utils/python"
import { retryUnlessDenied } from "./access"
import { trajectoryApi } from "./endpoints"
import { trajectoryKeys } from "./keys"

export type Unavailable = "pending" | "deleted" | "corrupt"

/** One reference read. It names its digest, so answers can be matched without their query keys. */
export type BlobAnswer =
  | { sha256: string; availability: "available"; value: unknown }
  | { sha256: string; availability: Unavailable }

/** References inside referenced content are followed this many levels deep. */
export const MAX_REF_NESTING = 8
/** Distinct references one value may pull in. */
export const MAX_REFS = 5_000
/** Blob reads in flight at once across the page; the others wait for a free slot. */
export const MAX_BLOB_READS = 6
/** Containers nested deeper are not searched; the worker externalizes values at depth 6 at most. */
const MAX_DEPTH = 64

const UNAVAILABLE: Readonly<Record<number, Unavailable>> = { 404: "pending", 410: "deleted", 409: "corrupt" }
/** Deleted outranks corrupt, which outranks "not produced yet". */
const SEVERITY: Readonly<Record<Unavailable, number>> = { pending: 1, corrupt: 2, deleted: 3 }

export interface BlobTarget {
  viewer: string
  sessionId: string
  /** Where the reference was met; its content is the same at every watermark that shows it. */
  throughSeq: Seq
}

/** A value whose references cannot all be read: one has no content, or there are too many or they loop. */
export class UnresolvedContentError extends Error {
  constructor(readonly availability: Unavailable | "unsupported") {
    super(`Referenced content is ${availability}`)
    this.name = "UnresolvedContentError"
  }
}

/* ------------------------------- reading ------------------------------- */

let reading = 0
const waiting: Array<() => void> = []

function abortError(): DOMException {
  return new DOMException("The content read was aborted", "AbortError")
}

/** A read slot; resolves with its release. Aborting while waiting gives the place up. */
function acquireSlot(signal?: AbortSignal): Promise<() => void> {
  return new Promise((resolve, reject) => {
    const start = () => {
      reading += 1
      let released = false
      resolve(() => {
        if (released) return
        released = true
        reading -= 1
        waiting.shift()?.()
      })
    }
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    if (reading < MAX_BLOB_READS) {
      start()
      return
    }
    const turn = () => {
      signal?.removeEventListener("abort", leave)
      start()
    }
    const leave = () => {
      const index = waiting.indexOf(turn)
      if (index >= 0) waiting.splice(index, 1)
      reject(abortError())
    }
    signal?.addEventListener("abort", leave, { once: true })
    waiting.push(turn)
  })
}

/** One reference's JSON at `throughSeq`. 404, 410 and 409 are answers without content, not failures. */
export async function loadBlob(
  sessionId: string,
  sha256: string,
  throughSeq: Seq,
  signal?: AbortSignal,
): Promise<BlobAnswer> {
  const release = await acquireSlot(signal)
  try {
    const value = await trajectoryApi.blob(sessionId, sha256, throughSeq, signal)
    return { sha256, availability: "available", value }
  } catch (error) {
    const availability = error instanceof ApiError ? UNAVAILABLE[error.status] : undefined
    if (availability) return { sha256, availability }
    throw error
  } finally {
    release()
  }
}

export function blobQuery(target: BlobTarget, sha256: string, enabled = true) {
  return queryOptions({
    queryKey: trajectoryKeys.blob(target.viewer, target.sessionId, sha256),
    queryFn: ({ signal }) => loadBlob(target.sessionId, sha256, target.throughSeq, signal),
    enabled,
    // Content never changes under a digest; an answer without content may.
    staleTime: (query) => (query.state.data?.availability === "available" ? Infinity : 0),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryUnlessDenied,
  })
}

/* ------------------------------ resolution ------------------------------ */

export interface RefTree {
  /** The value with every answered reference replaced; the same object when nothing was replaced. */
  value: unknown
  /** Every distinct reference reached, in discovery order. */
  refs: string[]
  /** Reached references without an answer yet. */
  missing: string[]
  /** The most severe answer without content, if any. */
  unavailable: Unavailable | null
  /** Too many references, nested too deep or looping: the value cannot be completed. */
  overflow: boolean
}

export type BlobLookup = (sha256: string) => BlobAnswer | undefined

function walkRefs(root: unknown, lookup: BlobLookup, rebuild: boolean): RefTree {
  const reached = new Set<string>()
  const missing = new Set<string>()
  const resolved = new Map<string, unknown>()
  const expanding = new Set<string>()
  let unavailable: Unavailable | null = null
  let overflow = false

  function reference(envelope: RefEnvelope): unknown {
    const sha = envelope.$ref.sha256.toLowerCase()
    if (resolved.has(sha)) return resolved.get(sha)
    if (!reached.has(sha)) {
      if (reached.size >= MAX_REFS) {
        overflow = true
        return envelope
      }
      reached.add(sha)
    }
    const answer = lookup(sha)
    if (!answer) {
      missing.add(sha)
      return envelope
    }
    if (answer.availability !== "available") {
      if (unavailable === null || SEVERITY[answer.availability] > SEVERITY[unavailable])
        unavailable = answer.availability
      return envelope
    }
    if (expanding.has(sha) || expanding.size >= MAX_REF_NESTING) {
      overflow = true
      return envelope
    }
    expanding.add(sha)
    const value = walk(answer.value, 0)
    expanding.delete(sha)
    resolved.set(sha, value)
    return value
  }

  function walk(node: unknown, depth: number): unknown {
    if (isRefEnvelope(node)) return reference(node)
    if (depth >= MAX_DEPTH) return node
    if (Array.isArray(node)) {
      let items: unknown[] | null = null
      for (let index = 0; index < node.length; index += 1) {
        const item: unknown = node[index]
        const next = walk(item, depth + 1)
        if (rebuild && next !== item) {
          items ??= [...node]
          items[index] = next
        }
      }
      return items ?? node
    }
    if (!isPlainObject(node)) return node
    let fields: Record<string, unknown> | null = null
    for (const [key, item] of Object.entries(node)) {
      const next = walk(item, depth + 1)
      if (rebuild && next !== item) {
        fields ??= { ...node }
        fields[key] = next
      }
    }
    return fields ?? node
  }

  const value = walk(root, 0)
  return { value, refs: [...reached], missing: [...missing], unavailable, overflow }
}

/**
 * `root` with the references `lookup` can answer replaced by their content,
 * following references inside that content. Untouched containers are shared,
 * so a value without references comes back as the very same object.
 */
export function resolveTree(root: unknown, lookup: BlobLookup): RefTree {
  return walkRefs(root, lookup, true)
}

/** The references reachable from `root` through what `lookup` can answer; builds nothing. */
export function reachableRefs(root: unknown, lookup: BlobLookup): string[] {
  return walkRefs(root, lookup, false).refs
}

export function containsRefs(value: unknown): boolean {
  return walkRefs(value, () => undefined, false).refs.length > 0
}

/**
 * Every reference in `value` read, for a one-off use such as copy or save:
 * what the cache lacks is fetched round by round, nested references included.
 * Throws `UnresolvedContentError` when the value cannot be completed.
 */
export async function resolveValue(
  client: QueryClient,
  target: BlobTarget,
  value: unknown,
): Promise<unknown> {
  const lookup: BlobLookup = (sha) =>
    client.getQueryData<BlobAnswer>(trajectoryKeys.blob(target.viewer, target.sessionId, sha))
  for (let round = 0; round <= MAX_REF_NESTING; round += 1) {
    const tree = resolveTree(value, lookup)
    if (tree.overflow) throw new UnresolvedContentError("unsupported")
    if (tree.unavailable) throw new UnresolvedContentError(tree.unavailable)
    if (!tree.missing.length) return tree.value
    await Promise.all(tree.missing.map((sha) => client.fetchQuery(blobQuery(target, sha))))
  }
  throw new UnresolvedContentError("unsupported")
}
