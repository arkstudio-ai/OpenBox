// Sequence arithmetic on decimal strings. The server allocates BIGINT seqs and
// sends them as strings; `Number("9007199254740993")` silently rounds, which
// would merge two distinct events during de-duplication. Comparison works on
// normalised digit strings (length first, then lexicographic); the rare
// arithmetic goes through BigInt.
import type { Seq } from "../types/protocol"

const DIGITS = /^\d+$/

export const ZERO_SEQ: Seq = "0"

export class SeqError extends TypeError {
  constructor(value: unknown) {
    super(`Invalid sequence value: ${String(value)}`)
    this.name = "SeqError"
  }
}

export function isSeq(value: unknown): value is Seq {
  return typeof value === "string" && DIGITS.test(value)
}

function normal(value: Seq): string {
  if (!isSeq(value)) throw new SeqError(value)
  return value.replace(/^0+(?=\d)/, "")
}

/** Parse anything the wire or the URL might carry into a canonical seq, or null. */
export function toSeq(value: unknown): Seq | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? String(value) : null
  if (typeof value === "bigint") return value >= BigInt(0) ? value.toString() : null
  return isSeq(value) ? normal(value) : null
}

export function cmpSeq(a: Seq, b: Seq): -1 | 0 | 1 {
  const x = normal(a)
  const y = normal(b)
  if (x.length !== y.length) return x.length < y.length ? -1 : 1
  if (x === y) return 0
  return x < y ? -1 : 1
}

export const eqSeq = (a: Seq, b: Seq) => cmpSeq(a, b) === 0
export const ltSeq = (a: Seq, b: Seq) => cmpSeq(a, b) < 0
export const lteSeq = (a: Seq, b: Seq) => cmpSeq(a, b) <= 0
export const gtSeq = (a: Seq, b: Seq) => cmpSeq(a, b) > 0
export const gteSeq = (a: Seq, b: Seq) => cmpSeq(a, b) >= 0

export function maxSeq(a: Seq, b: Seq): Seq {
  return cmpSeq(a, b) >= 0 ? normal(a) : normal(b)
}

export function minSeq(a: Seq, b: Seq): Seq {
  return cmpSeq(a, b) <= 0 ? normal(a) : normal(b)
}

/** `seq + delta`, clamped at zero. */
export function addSeq(seq: Seq, delta: number): Seq {
  const next = BigInt(normal(seq)) + BigInt(delta)
  return next < BigInt(0) ? ZERO_SEQ : next.toString()
}

/** Sorts ascending without mutating; stable for equal seqs. */
export function sortBySeq<T>(items: readonly T[], seqOf: (item: T) => Seq): T[] {
  return [...items].sort((a, b) => cmpSeq(seqOf(a), seqOf(b)))
}

/** Index of the last item whose seq is ≤ target, or -1. Items must be seq-sorted. */
export function lastIndexAtOrBefore<T>(items: readonly T[], target: Seq, seqOf: (item: T) => Seq): number {
  let lo = 0
  let hi = items.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (lteSeq(seqOf(items[mid]), target)) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}
