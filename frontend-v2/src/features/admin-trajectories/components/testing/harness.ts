// Test-only helpers for presentation tests of the trajectory inspector. Kept
// as plain TypeScript (no JSX) and imported only from *.test.tsx files.
import { createElement, type ReactNode } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { PROJECTOR_VERSION, type TrajectoryRecord } from "../../types/protocol"
import { statistics } from "../../utils/statistics"
import { buildTree, type ViewRecord } from "../../utils/view"
import { InspectorContext, type InspectorEnv } from "../inspector/context"
import { buildOrdinals } from "../session/ordinals"

export function makeRecord(
  partial: Partial<TrajectoryRecord> & { record_id: string; kind: string },
): TrajectoryRecord {
  return {
    title: partial.kind,
    preview: null,
    result_preview: null,
    status: "completed",
    status_reason: null,
    source_session_id: "ses_test",
    turn_id: null,
    run_id: null,
    generation: null,
    agent_id: null,
    parent_agent_id: null,
    step_id: null,
    request_id: null,
    call_id: null,
    parent_call_id: null,
    message_id: null,
    part_id: null,
    caused_by_event_id: null,
    start_seq: "1",
    end_seq: "2",
    as_of_seq: "2",
    started_at: "2026-09-11T08:00:00.000Z",
    finished_at: "2026-09-11T08:00:01.000Z",
    duration_ms: 1000,
    timing_source: "producer_monotonic",
    data: {},
    blocks: [],
    usage: {},
    ...partial,
  }
}

export function inspectorEnv(
  records: readonly TrajectoryRecord[] = [],
  overrides: Partial<InspectorEnv> = {},
): InspectorEnv {
  const map: Record<string, TrajectoryRecord> = Object.fromEntries(
    records.map((record) => [record.record_id, record]),
  )
  const tree = buildTree(records as ViewRecord[])
  return {
    sessionId: "ses_test",
    throughSeq: "10",
    clock: null,
    live: false,
    records: map,
    tree,
    events: [],
    statistics: statistics({
      projector_version: PROJECTOR_VERSION,
      through_seq: "10",
      records: map,
      unsupported_events: [],
      coverage_start: null,
    }),
    ordinals: buildOrdinals(tree.nodes.values()),
    select: () => undefined,
    ...overrides,
  }
}

export function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
}

/** Wraps a node in Query, a memory router and the inspector context. */
export function withInspector(
  node: ReactNode,
  env: InspectorEnv,
  client: QueryClient = testClient(),
): ReactNode {
  return createElement(
    QueryClientProvider,
    { client },
    createElement(MemoryRouter, null, createElement(InspectorContext.Provider, { value: env }, node)),
  )
}
