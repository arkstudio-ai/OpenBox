import { useCallback, useId, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { MousePointerClick } from "lucide-react"
import { ApiError } from "@/shared/api/http"
import { Spinner } from "@/shared/ui/Spinner"
import { useRecordDetail } from "../../api/queries"
import type { Seq } from "../../types/protocol"
import { lteSeq } from "../../utils/seq"
import { tabsFor, type TabId } from "../../utils/tabs"
import { ancestry, type ViewRecord } from "../../utils/view"
import { useSettledSeq } from "../session/useSettledSeq"
import { useInspector } from "./context"
import { EnvelopeRendererContext } from "./envelopeContext"
import { InspectorHeader } from "./InspectorHeader"
import { InspectorTabs } from "./InspectorTabs"
import type { ContentEnvelope } from "./media"
import { MediaRefView } from "./MediaRefView"
import { TabPanel } from "./TabPanel"
import { panelDomId, tabDomId } from "./tabIds"
import { NS, type InspectedRecord } from "./types"

interface RecordInspectorProps {
  selectedId: string | null
  /** The position is moving quickly (live or playing): refresh details at most a few times a second. */
  settle: boolean
}

interface EmptyStateProps {
  text: string
}

interface InspectorBodyProps {
  recordId: string
  local: ViewRecord
  settle: boolean
  tab: TabId | null
  onTab: (tab: TabId) => void
}

interface Shown {
  id: string
  seq: Seq
  record: InspectedRecord
}

const SETTLE_MS = 400

function EmptyState({ text }: EmptyStateProps) {
  return (
    <div
      className="text-n500 flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-xs"
      data-testid="trajectory-inspector-empty"
    >
      <MousePointerClick size={18} aria-hidden />
      {text}
    </div>
  )
}

function InspectorBody({ recordId, local, settle, tab, onTab }: InspectorBodyProps) {
  const { t } = useTranslation(NS)
  const idBase = useId()
  const env = useInspector()
  const detailSeq = useSettledSeq(env.throughSeq, settle ? SETTLE_MS : 0)
  const detail = useRecordDetail(env.sessionId, detailSeq, recordId, true)
  const [shown, setShown] = useState<Shown | null>(null)
  const fresh = detail.data?.record
  if (fresh && shown?.record !== fresh) setShown({ id: recordId, seq: detailSeq, record: fresh })
  const chain = useMemo(() => ancestry(env.tree, recordId), [env.tree, recordId])

  const failed = !!detail.error
  const notFound = detail.error instanceof ApiError && detail.error.status === 404
  // Earlier details may bridge a refresh; later ones (after a backwards seek) never do.
  const previous = shown && shown.id === recordId && lteSeq(shown.seq, detailSeq) ? shown.record : null
  const record = failed ? null : (fresh ?? previous)
  const tabs = tabsFor(record ?? local)
  const active = tab && tabs.includes(tab) ? tab : tabs[0]

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="trajectory-inspector"
      data-record-id={recordId}
      data-detail-seq={detailSeq}
    >
      <InspectorHeader record={local} chain={chain} updating={!fresh && !!previous} />
      <InspectorTabs tabs={tabs} active={active} onChange={onTab} idBase={idBase} />
      <div
        id={panelDomId(idBase)}
        role="tabpanel"
        aria-labelledby={tabDomId(idBase, active)}
        tabIndex={0}
        className="min-h-0 flex-1 overflow-auto p-4"
        data-testid="trajectory-inspector-panel"
      >
        {notFound && <p className="text-n600 text-xs">{t("inspector.notYet", { seq: detailSeq })}</p>}
        {failed && !notFound && (
          <div role="alert" className="text-dangerink flex items-center gap-2 text-xs">
            {t("inspector.failed")}
            <button type="button" className="underline" onClick={() => void detail.refetch()}>
              {t("common.retry")}
            </button>
          </div>
        )}
        {!failed && !record && (
          <span className="text-n500 inline-flex items-center gap-2 text-xs">
            <Spinner className="size-3.5" />
            {t("inspector.loading", { seq: detailSeq })}
          </span>
        )}
        {record && <TabPanel record={record} tab={active} />}
      </div>
    </div>
  )
}

/**
 * Detail of the selected record read from the server at the shown position.
 * While a newer read is on its way the previous details may stay — but only
 * if they describe an earlier position, never a later one. A record that does
 * not exist yet at the position keeps its selection and says so.
 */
export function RecordInspector({ selectedId, settle }: RecordInspectorProps) {
  const { t } = useTranslation(NS)
  const env = useInspector()
  const [tab, setTab] = useState<TabId | null>(null)
  const renderEnvelope = useCallback((value: ContentEnvelope) => <MediaRefView value={value} />, [])
  if (!selectedId) return <EmptyState text={t("inspector.empty")} />
  const local = env.records[selectedId]
  if (!local) return <EmptyState text={t("inspector.notYet", { seq: env.throughSeq })} />
  return (
    <EnvelopeRendererContext.Provider value={renderEnvelope}>
      <InspectorBody recordId={selectedId} local={local} settle={settle} tab={tab} onTab={setTab} />
    </EnvelopeRendererContext.Provider>
  )
}
