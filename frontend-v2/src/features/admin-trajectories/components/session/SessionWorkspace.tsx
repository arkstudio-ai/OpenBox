import { useCallback, useMemo, useState, type CSSProperties } from "react"
import { useTranslation } from "react-i18next"
import type { Position, SyncSnapshot } from "../../api/sync"
import { useTrajectoryView } from "../../stores/view"
import type { Seq } from "../../types/protocol"
import type { DetailParams } from "../../utils/params"
import { clockAt } from "../../utils/playback"
import { lastIndexAtOrBefore } from "../../utils/seq"
import { statistics } from "../../utils/statistics"
import {
  agentTree,
  ancestry,
  buildTree,
  flattenTree,
  type AgentTreeNode,
  type RecordFilters,
} from "../../utils/view"
import { InspectorContext, type InspectorEnv } from "../inspector/context"
import { RecordInspector } from "../inspector/RecordInspector"
import { AgentTreePanel } from "./AgentTreePanel"
import { shortId } from "./format"
import { InspectorResizer } from "./InspectorResizer"
import { buildOrdinals } from "./ordinals"
import { PlaybackBar } from "./PlaybackBar"
import { RecordTable } from "./RecordTable"
import type { RowContext } from "./RecordRowView"
import { RecordToolbar, type ToolbarOptions } from "./RecordToolbar"
import { SearchPanel } from "./SearchPanel"
import { StatisticsStrip } from "./StatisticsStrip"
import { TimelinePanel } from "./TimelinePanel"
import { useNow } from "./useNow"
import { usePlaybackControls } from "./usePlaybackControls"

type ReadyPosition = Extract<Position, { status: "ready" }>

interface SessionWorkspaceProps {
  sessionId: string
  snapshot: SyncSnapshot
  position: ReadyPosition
  detail: DetailParams
  onDetail: (patch: Partial<DetailParams>) => void
}

function flattenAgents(
  nodes: readonly AgentTreeNode[],
  mainLabel: string,
  depth = 0,
): Array<{ id: string; label: string }> {
  return nodes.flatMap((node) => [
    {
      id: node.agentId,
      label: `${"· ".repeat(depth)}${node.name ?? (depth === 0 && !node.recordId ? mainLabel : shortId(node.agentId))}`,
    },
    ...flattenAgents(node.children, mainLabel, depth + 1),
  ])
}

function sortedValues(values: Iterable<string | null>): string[] {
  return [...new Set([...values].filter((value): value is string => !!value))].sort()
}

/**
 * Everything at one position: totals, playback, timeline, the record table
 * with agents, filters and search, and the resizable inspector. Every region
 * reads the same projection and the same watermark.
 */
export function SessionWorkspace({ sessionId, snapshot, position, detail, onDetail }: SessionWorkspaceProps) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const live = useTrajectoryView((s) => s.playhead === null)
  const playing = useTrajectoryView((s) => s.playing)
  const selectedId = useTrajectoryView((s) => s.selectedRecordId)
  const collapsed = useTrajectoryView((s) => s.collapsed)
  const timeMode = useTrajectoryView((s) => s.timeMode)
  const scale = useTrajectoryView((s) => s.timelineScale)
  const width = useTrajectoryView((s) => s.inspectorWidth)
  const select = useTrajectoryView((s) => s.select)
  const toggleCollapsed = useTrajectoryView((s) => s.toggleCollapsed)
  const setScale = useTrajectoryView((s) => s.setTimelineScale)
  const setWidth = useTrajectoryView((s) => s.setInspectorWidth)
  const setPlayhead = useTrajectoryView((s) => s.setPlayhead)
  const [searchOpen, setSearchOpen] = useState(false)

  const { state, seq } = position
  const records = state.records
  const all = useMemo(() => Object.values(records), [records])
  const tree = useMemo(() => buildTree(all), [all])
  const ordinals = useMemo(() => buildOrdinals(tree.nodes.values()), [tree])
  const stats = useMemo(() => statistics(state), [state])
  const agents = useMemo(() => agentTree(all), [all])
  const filters = useMemo<RecordFilters>(
    () => ({ kinds: detail.kinds, statuses: detail.statuses, agentId: detail.agentId, text: detail.text }),
    [detail.agentId, detail.kinds, detail.statuses, detail.text],
  )
  const rows = useMemo(() => flattenTree(tree, collapsed, filters), [collapsed, filters, tree])
  const events = useMemo(
    () => snapshot.events.slice(0, lastIndexAtOrBefore(snapshot.events, seq, (event) => event.seq) + 1),
    [seq, snapshot.events],
  )
  const now = useNow(live)
  const clock = live ? now : clockAt(events, seq)
  const controls = usePlaybackControls(snapshot, position)

  // Selecting from the timeline or search unfolds the groups hiding the record.
  const reveal = useCallback(
    (recordId: string) => {
      for (const ancestor of ancestry(tree, recordId))
        if (collapsed[ancestor.record_id]) toggleCollapsed(ancestor.record_id)
      select(recordId)
    },
    [collapsed, select, toggleCollapsed, tree],
  )
  const moveTo = useCallback(
    (target: Seq, recordId: string) => {
      setPlayhead(target)
      select(recordId)
    },
    [select, setPlayhead],
  )

  const env = useMemo<InspectorEnv>(
    () => ({
      sessionId,
      throughSeq: seq,
      clock,
      live,
      records,
      tree,
      events,
      statistics: stats,
      ordinals,
      select: reveal,
    }),
    [clock, events, live, ordinals, records, reveal, seq, sessionId, stats, tree],
  )
  const agentNames = useMemo(
    () =>
      new Map(
        all.flatMap((record) =>
          record.kind === "agent" && record.agent_id ? [[record.agent_id, record.title] as const] : [],
        ),
      ),
    [all],
  )
  const rowContext = useMemo<RowContext>(
    () => ({ ordinals, clock, timeMode, locale: i18n.language, agentNames }),
    [agentNames, clock, i18n.language, ordinals, timeMode],
  )
  const options = useMemo<ToolbarOptions>(
    () => ({
      kinds: sortedValues(all.map((record) => record.kind)),
      statuses: sortedValues(all.map((record) => record.status)),
      agents: flattenAgents(agents, t("agents.main")),
    }),
    [agents, all, t],
  )

  return (
    <InspectorContext.Provider value={env}>
      <StatisticsStrip statistics={stats} />
      <PlaybackBar model={controls.model} actions={controls.actions} />
      <TimelinePanel
        records={all}
        seq={seq}
        clock={clock}
        selectedId={selectedId}
        onSelect={reveal}
        scale={scale}
        onScale={setScale}
      />
      <div
        className="border-hair bg-card @container/workspace flex min-w-0 flex-col overflow-hidden rounded-xl border lg:h-[calc(100dvh-7rem)] lg:min-h-[34rem] lg:flex-row"
        style={{ "--inspector-width": `${width}px` } as CSSProperties}
        data-testid="trajectory-workspace"
      >
        <AgentTreePanel
          nodes={agents}
          activeAgentId={filters.agentId}
          onFilter={(agentId) => onDetail({ agentId })}
          onSelect={reveal}
          className="border-hair hidden w-52 flex-none border-e @min-[80rem]/workspace:flex"
        />
        <div className="flex h-[32rem] min-w-0 flex-1 flex-col lg:h-auto">
          <RecordToolbar
            filters={filters}
            options={options}
            onFilters={(patch) =>
              onDetail({
                ...(patch.kinds ? { kinds: [...patch.kinds] } : {}),
                ...(patch.statuses ? { statuses: [...patch.statuses] } : {}),
                ...("agentId" in patch ? { agentId: patch.agentId ?? null } : {}),
                ...(patch.text !== undefined ? { text: patch.text } : {}),
              })
            }
            counts={{ shown: rows.length, total: all.length }}
            searchOpen={searchOpen}
            onToggleSearch={() => setSearchOpen((open) => !open)}
          />
          {searchOpen && (
            <SearchPanel
              sessionId={sessionId}
              seq={seq}
              liveSeq={snapshot.loadedSeq}
              live={live}
              records={records}
              onSelect={reveal}
              onMove={moveTo}
            />
          )}
          <RecordTable
            rows={rows}
            selectedId={selectedId}
            onSelect={select}
            onToggle={toggleCollapsed}
            context={rowContext}
          />
        </div>
        <InspectorResizer width={width} onResize={setWidth} className="hidden lg:block" />
        <aside
          aria-label={t("inspector.title")}
          className="border-hair flex h-[36rem] min-w-0 flex-col border-t lg:h-auto lg:w-[min(var(--inspector-width),50%)] lg:flex-none lg:border-t-0"
        >
          <RecordInspector selectedId={selectedId} settle={live || playing} />
        </aside>
      </div>
    </InspectorContext.Provider>
  )
}
