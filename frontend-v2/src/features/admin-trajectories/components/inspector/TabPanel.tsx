import type { ComponentType } from "react"
import { TAB_FIELDS, type TabFields } from "../../constants/inspector"
import type { TabId } from "../../utils/tabs"
import { AgentTreeTab } from "./panels/AgentTreeTab"
import { ArtifactDiffPanel } from "./panels/ArtifactDiffPanel"
import { ArtifactPreviewPanel } from "./panels/ArtifactPreviewPanel"
import { ArtifactVersionsPanel } from "./panels/ArtifactVersionsPanel"
import { AssistantPreviewPanel } from "./panels/AssistantPreviewPanel"
import { CapturedFieldsPanel } from "./panels/CapturedFieldsPanel"
import { EventsPanel } from "./panels/EventsPanel"
import { InputPreviewPanel } from "./panels/InputPreviewPanel"
import { InterruptImpactPanel } from "./panels/InterruptImpactPanel"
import { JobProgressPanel } from "./panels/JobProgressPanel"
import { QuestionsPanel } from "./panels/QuestionsPanel"
import { RawContentPanel } from "./panels/RawContentPanel"
import { RelatedPanel } from "./panels/RelatedPanel"
import { RequestInputPanel } from "./panels/RequestInputPanel"
import { RequestOptionsPanel } from "./panels/RequestOptionsPanel"
import { RetryAttemptsPanel } from "./panels/RetryAttemptsPanel"
import { SourcePanel } from "./panels/SourcePanel"
import { StateContentPanel } from "./panels/StateContentPanel"
import { SummaryPanel } from "./panels/SummaryPanel"
import { SystemDiffPanel } from "./panels/SystemDiffPanel"
import { SystemPromptPanel } from "./panels/SystemPromptPanel"
import { TimingPanel } from "./panels/TimingPanel"
import { ToolArgumentsPanel } from "./panels/ToolArgumentsPanel"
import { ToolCatalogPanel } from "./panels/ToolCatalogPanel"
import { ToolResultPanel } from "./panels/ToolResultPanel"
import { ToolSchemaPanel } from "./panels/ToolSchemaPanel"
import { UsagePanel } from "./panels/UsagePanel"
import type { InspectedRecord, PanelProps } from "./types"

interface TabPanelProps {
  record: InspectedRecord
  tab: TabId
}

type Panel = ComponentType<PanelProps>

const NONE: TabFields = { keys: [] }

/** A tab that lists the kind's captured fields for it (constants/inspector TAB_FIELDS). */
function capturedTab(tab: TabId): Panel {
  function CapturedTab({ record }: PanelProps) {
    return <CapturedFieldsPanel record={record} fields={TAB_FIELDS[`${record.kind}.${tab}`] ?? NONE} />
  }
  return CapturedTab
}

const BY_TAB: Readonly<Record<TabId, Panel>> = {
  summary: SummaryPanel,
  preview: InputPreviewPanel,
  raw: RawContentPanel,
  source: SourcePanel,
  input: capturedTab("input"),
  options: RequestOptionsPanel,
  usage: UsagePanel,
  timing: TimingPanel,
  arguments: ToolArgumentsPanel,
  result: capturedTab("result"),
  schema: ToolSchemaPanel,
  diff: capturedTab("diff"),
  systemPrompt: SystemPromptPanel,
  tools: ToolCatalogPanel,
  task: capturedTab("task"),
  tree: AgentTreeTab,
  request: capturedTab("request"),
  decision: capturedTab("decision"),
  questions: QuestionsPanel,
  answer: capturedTab("answer"),
  impact: InterruptImpactPanel,
  related: RelatedPanel,
  attempts: RetryAttemptsPanel,
  error: capturedTab("error"),
  output: capturedTab("output"),
  progress: JobProgressPanel,
  content: StateContentPanel,
  versions: ArtifactVersionsPanel,
  events: EventsPanel,
}

/** Typed views that replace the generic one for a particular kind. */
const BY_KIND_TAB: Readonly<Record<string, Panel>> = {
  "assistant.preview": AssistantPreviewPanel,
  "artifact.preview": ArtifactPreviewPanel,
  "artifact.diff": ArtifactDiffPanel,
  "request.input": RequestInputPanel,
  "tool.result": ToolResultPanel,
  "system.diff": SystemDiffPanel,
  "tool_catalog.diff": SystemDiffPanel,
}

export function TabPanel({ record, tab }: TabPanelProps) {
  const Panel = BY_KIND_TAB[`${record.kind}.${tab}`] ?? BY_TAB[tab]
  return <Panel record={record} />
}
