import { useCallback, useMemo, useState } from "react"
import { useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query"
import { trajectoryKeys } from "../../api/keys"
import { useAccessScope } from "../../api/queries"
import { blobQuery, reachableRefs, resolveTree, type BlobAnswer, type Unavailable } from "../../api/refs"
import { lteSeq } from "../../utils/seq"
import { useInspector } from "./context"
import type { InspectedRecord } from "./types"

export type Resolution<T> =
  | { status: "ready"; value: T }
  | { status: "loading" }
  | { status: "unavailable"; availability: Unavailable | "unsupported" }
  | { status: "error" }

export interface Resolved<T> {
  resolution: Resolution<T>
  /** Reads again every reference whose read failed. */
  retry: () => void
}

interface Answers {
  content: ReadonlyMap<string, BlobAnswer>
  failed: boolean
}

function collectAnswers(results: ReadonlyArray<UseQueryResult<BlobAnswer>>): Answers {
  const content = new Map<string, BlobAnswer>()
  for (const result of results) if (result.data) content.set(result.data.sha256, result.data)
  return { content, failed: results.some((result) => result.isError) }
}

function pick(data: Record<string, unknown> | undefined, fields: readonly string[]): Record<string, unknown> {
  const source = data ?? {}
  return Object.fromEntries(
    fields
      .filter((key) => Object.prototype.hasOwnProperty.call(source, key))
      .map((key) => [key, source[key]]),
  )
}

/**
 * `value` with its `$ref` values read at the inspector's watermark and put in
 * place (SPEC §11.2). A value without references is ready at once, as the same
 * object. Otherwise it is ready once every reference — references inside
 * referenced content included — has been read. Answers are cached by digest,
 * so another render, panel or record needing the same content reads nothing.
 */
export function useResolvedValue<T>(value: T): Resolved<T> {
  const { sessionId, throughSeq } = useInspector()
  const { viewer, allowed } = useAccessScope()
  const client = useQueryClient()
  // What the answers cached now can reach; every new answer re-renders and may reach further.
  const reached = reachableRefs(value, (sha) =>
    client.getQueryData<BlobAnswer>(trajectoryKeys.blob(viewer, sessionId, sha)),
  )
  const answers = useQueries({
    queries: reached.map((sha) => blobQuery({ viewer, sessionId, throughSeq }, sha, allowed)),
    combine: collectAnswers,
  })
  const resolution = useMemo((): Resolution<T> => {
    const tree = resolveTree(value, (sha) => answers.content.get(sha))
    if (tree.overflow) return { status: "unavailable", availability: "unsupported" }
    if (tree.unavailable) return { status: "unavailable", availability: tree.unavailable }
    if (answers.failed) return { status: "error" }
    if (tree.missing.length) return { status: "loading" }
    return { status: "ready", value: tree.value as T }
  }, [answers, value])
  const retry = useCallback(() => {
    void client.refetchQueries({
      queryKey: trajectoryKeys.blobs(viewer, sessionId),
      predicate: (query) => query.state.status === "error",
    })
  }, [client, sessionId, viewer])
  return { resolution, retry }
}

/**
 * The record as panels expect it, its `$ref` values read: only `fields` of its
 * data (for a panel that shows nothing else) or the whole record. While a newer
 * detail of the same record is being resolved, the previously resolved one
 * stays — but only if it describes the same or an earlier position.
 */
export function useResolvedRecord(
  record: InspectedRecord,
  fields?: readonly string[],
): Resolved<InspectedRecord> {
  // Keyed by the names rather than the array, so an inline list cannot make every render a new subject.
  const names = fields?.join("\n")
  const subject = useMemo<unknown>(
    () => (names === undefined ? record : pick(record.data, names.split("\n"))),
    [names, record],
  )
  const { resolution, retry } = useResolvedValue(subject)
  const resolved = useMemo((): InspectedRecord | null => {
    if (resolution.status !== "ready") return null
    if (resolution.value === subject) return record
    if (names === undefined) return resolution.value as InspectedRecord
    return { ...record, data: { ...record.data, ...(resolution.value as Record<string, unknown>) } }
  }, [names, record, resolution, subject])
  const [shown, setShown] = useState<InspectedRecord | null>(null)
  if (resolved && shown !== resolved) setShown(resolved)

  switch (resolution.status) {
    case "ready":
      return { resolution: { status: "ready", value: resolved ?? record }, retry }
    case "loading": {
      const bridge =
        shown !== null && shown.record_id === record.record_id && lteSeq(shown.as_of_seq, record.as_of_seq)
      return { resolution: bridge ? { status: "ready", value: shown } : resolution, retry }
    }
    default:
      return { resolution, retry }
  }
}
