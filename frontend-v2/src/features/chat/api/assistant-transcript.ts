import { useCallback, useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react"
import { useQueries, useQueryClient, type Query, type QueryClient, type UseQueryResult } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { assistantKeys, scopedOptions } from "./assistant"
import { useStreamStore } from "../stores/stream"
import { createHistoryProofReader, historyProofBarrier, historySourceBatches, requireFreshHistoryProof, subscribeHistoryProofBarrier } from "./history-source-proof"

export interface TranscriptPage { messages: MessageWithParts[] }
const EMPTY: MessageWithParts[] = []
interface Domain { owner: QueryClient; scope: string; foreground: number; barrier: number }
interface Receipt extends Domain { order: number; signal: AbortSignal }
interface HeldProjection { message: MessageWithParts; receipt: Receipt; batch: string; page: TranscriptPage; origin: Query }
// Only the successful queryFn can issue a carry receipt. Retained stream data,
// copied projections and cache timestamps cannot issue one.
const receipts = new WeakMap<TranscriptPage, { receipt: Receipt; carry: boolean }>()
let requestOrder = 0

export function reconcileTranscript(previous: TranscriptPage | undefined, next: TranscriptPage): TranscriptPage {
  const held = new Map(previous?.messages.map((message) => [message.id, message]) ?? [])
  let mixed = false
  const page = { messages: next.messages.map((message) => {
    const current = held.get(message.id)
    if (current && (current.source_checked_at ?? "") > (message.source_checked_at ?? "")) { mixed = true; return current }
    return message
  }) }
  const proof = receipts.get(next)
  const before = previous && receipts.get(previous)
  // Keeping a newer copy-time projection is correct for display, but this
  // response did not verify the resulting mixed page for carry into a new key.
  if (proof) receipts.set(page, { receipt: proof.receipt, carry: proof.carry && !mixed })
  // A click-time update may tighten a current projection, but cannot extend its
  // old authority epoch or grant carry into another batch.
  else if (before) receipts.set(page, { receipt: before.receipt, carry: false })
  return page
}

function sameDomain(receipt: Receipt, domain: Domain) {
  return receipt.owner === domain.owner && receipt.scope === domain.scope && receipt.foreground === domain.foreground &&
    receipt.barrier === domain.barrier && !receipt.signal.aborted
}

function commitHeld(held: Map<string, HeldProjection>, next: Map<string, HeldProjection>) {
  held.clear()
  for (const [id, projection] of next) held.set(id, projection)
}

function pruneRejections(rejected: Map<string, number>, chunks: string[][]) {
  const loaded = new Set(chunks.flat())
  for (const id of rejected.keys()) if (!loaded.has(id)) rejected.delete(id)
}

function commitBorrowed(held: Map<Query, TranscriptPage>, next: Map<Query, TranscriptPage>) {
  held.clear()
  for (const [origin, page] of next) held.set(origin, page)
}

function transcriptView(results: UseQueryResult<TranscriptPage>[], chunks: string[][], held: Map<string, HeldProjection>,
  context: { domain: Domain; keyPrefix: readonly unknown[]; rejected: Map<string, number>; scopePending: boolean }) {
  const messages: MessageWithParts[] = [], pendingIds = new Set<string>(), unavailableIds = new Set<string>()
  const retain = new Map<string, HeldProjection>()
  const borrowed = new Map<Query, TranscriptPage>()
  const valid = (receipt: Receipt, id: string) => sameDomain(receipt, context.domain) && receipt.order > (context.rejected.get(id) ?? 0)
  results.forEach((result, index) => {
    const selected = chunks[index], batch = selected.join(",")
    const origin = context.domain.owner.getQueryCache().find({ queryKey: [...context.keyPrefix, batch], exact: true })
    const proof = result.data && receipts.get(result.data)
    const current = new Map(result.data?.messages.map((message) => [message.id, message]))
    const fetched = result.isFetchedAfterMount && result.dataUpdatedAt >= context.domain.foreground
    for (const id of selected) {
      if (result.error) { unavailableIds.add(id); continue }
      if (context.scopePending) { pendingIds.add(id); continue }
      const checked = proof && fetched && valid(proof.receipt, id) ? current.get(id) : undefined
      if (checked) {
        messages.push(checked)
        if (proof!.carry && origin) retain.set(id, { message: checked, receipt: proof!.receipt, batch, page: result.data!, origin })
        continue
      }
      const before = held.get(id)
      // Preserve only a committed intersection while the replacement batch is
      // actually validating. An idle/cancelled or failed query cannot borrow it.
      if (result.fetchStatus === "fetching" && before && before.batch !== batch && valid(before.receipt, id) &&
        context.domain.owner.getQueryCache().get(before.origin.queryHash) === before.origin && before.origin.state.data === before.page) {
        messages.push(before.message); retain.set(id, before); borrowed.set(before.origin, before.page)
      } else pendingIds.add(id)
    }
  })
  return { messages, pendingIds, unavailableIds, retain, borrowed,
    failed: results.some((result) => !!result.error), scopePending: context.scopePending,
    pending: context.scopePending || pendingIds.size > 0 }
}

export async function readAssistantMessages(sessionId: string, ids: string[], workspaceId: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ session_id: sessionId })
  ids.forEach((id) => params.append("message_ids", id))
  const page = await http.get<TranscriptPage>(`/api/assistant/messages?${params}`, scopedOptions(workspaceId, signal))
  if (page.messages.length !== new Set(ids).size || new Set(page.messages.map((m) => m.id)).size !== page.messages.length ||
    page.messages.some((m) => !ids.includes(m.id) || m.session_id !== sessionId || !m.source_checked_at ||
      !["available", "pending", "unavailable"].includes(m.source_status ?? ""))) throw new Error("Missing current-source projection")
  return page
}

/** Recheck every loaded page; the current-source projection belongs to Query. */
export function useAssistantTranscript(sessionId: string) {
  const qc = useQueryClient()
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const [foreground, setForeground] = useState(() => ({ visible: document.visibilityState === "visible", epoch: Date.now() }))
  const [readHistoryProof] = useState(createHistoryProofReader)
  // Like snapshot denial floors, this mount-local map records only committed
  // projections. Updating it needs no render: Query owns every visible change.
  const [held] = useState(() => new Map<string, HeldProjection>())
  const [borrowed] = useState(() => new Map<Query, TranscriptPage>())
  const [rejected, setRejected] = useState(() => new Map<string, number>())
  const subscribe = useCallback((listener: () => void) => subscribeHistoryProofBarrier(qc, { userId, workspaceId }, sessionId, listener),
    [qc, userId, workspaceId, sessionId])
  const getBarrier = useCallback(() => historyProofBarrier(qc, { userId, workspaceId }, sessionId), [qc, userId, workspaceId, sessionId])
  const barrier = useSyncExternalStore(subscribe, getBarrier)
  const domain: Domain = { owner: qc, scope: JSON.stringify([userId, workspaceId, sessionId]), foreground: foreground.epoch, barrier }
  useEffect(() => qc.getQueryCache().subscribe((event) => {
    const page = borrowed.get(event.query)
    if (!page || event.type !== "removed" && event.query.state.data === page) return
    borrowed.delete(event.query)
    // A copied/rechecked old page may no longer have a Query observer. A
    // change while it is borrowed still invalidates the pending replacement,
    // including the gap between queryFn returning and Query publishing it.
    requireFreshHistoryProof(qc, { userId, workspaceId }, sessionId)
  }), [qc, borrowed, userId, workspaceId, sessionId])
  useEffect(() => {
    const changed = () => {
      requireFreshHistoryProof(qc, { userId, workspaceId }, sessionId)
      setForeground({ visible: document.visibilityState === "visible", epoch: Date.now() })
    }
    document.addEventListener("visibilitychange", changed)
    return () => document.removeEventListener("visibilitychange", changed)
  }, [qc, userId, workspaceId, sessionId])
  const messages = useStreamStore((state) => state.messages.get(sessionId) ?? EMPTY)
  const chunks = historySourceBatches(messages)
  const results = useQueries({ queries: chunks.map((selected) => ({
    queryKey: assistantKeys.transcript(userId, workspaceId, sessionId, selected),
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const scope = { userId, workspaceId }
      const barrier = historyProofBarrier(qc, scope, sessionId)
      const receipt: Receipt = { ...domain, barrier, order: ++requestOrder, signal }
      const reject = () => setRejected((previous) => {
        if (selected.every((id) => (previous.get(id) ?? 0) >= receipt.order)) return previous
        const next = new Map(previous)
        for (const id of selected) next.set(id, Math.max(next.get(id) ?? 0, receipt.order))
        return next
      })
      signal.addEventListener("abort", reject, { once: true })
      try {
        const held = new Map((useStreamStore.getState().messages.get(sessionId) ?? []).map((message) => [message.id, message]))
        const history = readHistoryProof(selected.map((id) => {
          const message = held.get(id)
          return message?.session_id === sessionId ? message : undefined
        }), scope, Math.max(foreground.epoch, barrier))
        // The history endpoint already performed the identical current-source
        // check. Reuse each new response once; a mixed batch only fetches the
        // missing originals, regardless of how many history responses formed it.
        const checked = new Map(history.map((message) => [message.id, message]))
        const missing = selected.filter((id) => !checked.has(id))
        if (missing.length) {
          const page = await readAssistantMessages(sessionId, missing, workspaceId, signal)
          for (const message of page.messages) checked.set(message.id, message)
        }
        if (signal.aborted || historyProofBarrier(qc, scope, sessionId) !== barrier) {
          throw new Error("History source proof changed during refresh")
        }
        const page = { messages: selected.map((id) => checked.get(id)!) }
        receipts.set(page, { receipt, carry: true })
        return page
      } catch (error) { reject(); throw error }
      finally { signal.removeEventListener("abort", reject) }
    },
    structuralSharing: (previous: unknown, next: unknown) => reconcileTranscript(previous as TranscriptPage | undefined, next as TranscriptPage),
    enabled: !!workspaceId && userId !== "anonymous" && foreground.visible,
    staleTime: 0, refetchOnMount: "always" as const, refetchInterval: 15_000, retry: false,
  })) })
  const { retain, borrowed: nextBorrowed, ...view } = transcriptView(results, chunks, held, {
    domain, keyPrefix: assistantKeys.transcripts(userId, workspaceId, sessionId),
    rejected, scopePending: !workspaceId || userId === "anonymous" || !foreground.visible,
  })
  useLayoutEffect(() => {
    // A child layout effect can update an old page after render calculated the
    // intersection, before it becomes part of the subscribed borrowed set.
    const changed = [...nextBorrowed].some(([origin, page]) => qc.getQueryCache().get(origin.queryHash) !== origin || origin.state.data !== page)
    if (changed) {
      held.clear(); borrowed.clear()
      requireFreshHistoryProof(qc, { userId, workspaceId }, sessionId)
    } else {
      commitHeld(held, retain)
      commitBorrowed(borrowed, nextBorrowed)
    }
    pruneRejections(rejected, chunks)
  }, [qc, userId, workspaceId, sessionId, held, retain, borrowed, nextBorrowed, rejected, chunks])
  return view
}

/** A click-time read also refreshes any mounted transcript pages. */
export function refreshTranscriptPages(qc: QueryClient, key: readonly unknown[], page: TranscriptPage) {
  const updates = new Map(page.messages.map((message) => [message.id, message]))
  qc.setQueriesData<TranscriptPage>({ queryKey: key }, (cached) => cached && ({ messages: cached.messages.map((held) => {
    const next = updates.get(held.id)
    return next && (next.source_checked_at ?? "") >= (held.source_checked_at ?? "") ? next : held
  }) }))
}
