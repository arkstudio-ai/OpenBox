import type { FilePart, FileRelation, MessageWithParts, TextPart, ToolPart } from "@/shared/types/api"

export type ArtifactRole = NonNullable<FileRelation["role"]>

export interface WorkNarration {
  kind: "narration"
  id: string
  order: number
  text: string
}

export interface ArtifactGroup {
  kind: "artifact"
  id: string
  order: number
  artifactKind: string
  role: ArtifactRole
  label: string | null
  caption: string | null
  ordinal: number | null
  revision: number | null
  metadata: Record<string, unknown>
  sourceTool: ToolPart | null
  parts: FilePart[]
}

export type WorkEvent = WorkNarration | ArtifactGroup

export interface AssistantContentView {
  finalText: string
  finalMessageId: string | null
  hasFinal: boolean
  progress: WorkNarration[]
  workEvents: WorkEvent[]
  resultGroups: ArtifactGroup[]
  verification: ArtifactGroup | null
  incomplete: boolean
}

interface SegmentRecord {
  id?: string
  ordinal: number
  revision?: number
  script?: string
  transcript?: string
  sttVerdict?: string
  sttSimilarity?: number
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function textParts(message: MessageWithParts): TextPart[] {
  return message.parts.filter((part): part is TextPart => part.type === "text" && Boolean(part.text))
}

function isToolStepFinish(finish: string | null | undefined): boolean {
  return finish === "tool_calls" || finish === "tool-calls" || finish === "compact" || finish === "aborted"
}

/** Locate the one assistant step whose prose is the user-facing answer.
 *
 * New rows carry TextPart.channel.  Message.finish is the durable fallback for
 * rows written before that field existed.  A completely old transcript may
 * have no finish metadata at all; only in that case do we preserve the former
 * behaviour and treat its newest prose as the answer.
 */
function finalMessageIndex(messages: MessageWithParts[], streaming: boolean): number {
  let candidate = -1
  messages.forEach((message, index) => {
    const texts = textParts(message)
    if (texts.some((part) => part.channel === "final")) candidate = index
    else if (message.finish === "stop" && texts.some((part) => part.channel !== "commentary")) {
      candidate = index
    }
  })
  if (candidate >= 0) return candidate

  const newest = messages.length - 1
  if (streaming && newest >= 0) {
    const message = messages[newest]
    const hasText = textParts(message).some((part) => part.channel !== "commentary")
    const hasTool = message.parts.some((part) => part.type === "tool" || part.type === "subtask")
    if (hasText && !hasTool && !isToolStepFinish(message.finish)) return newest
  }

  // Compatibility for early OpenBox rows, which predate finish persistence.
  if (messages.every((message) => message.finish == null && message.error == null)) {
    for (let index = newest; index >= 0; index -= 1) {
      if (textParts(messages[index]).some((part) => part.channel == null)) return index
    }
  }
  return -1
}

function metadataAssetIds(tool: ToolPart): string[] {
  const metadata = tool.metadata ?? {}
  const ids: string[] = []
  const one = asString(metadata.asset_id)
  if (one) ids.push(one)
  if (Array.isArray(metadata.asset_ids)) {
    for (const value of metadata.asset_ids) {
      const id = asString(value)
      if (id) ids.push(id)
    }
  }
  // Older tool metadata was deliberately pruned before persistence, while
  // the human-readable result still retained asset_id=... references.
  for (const match of tool.output?.matchAll(/\basset_id=([^;\s]+)/g) ?? []) {
    const id = asString(match[1])
    if (id) ids.push(id)
  }
  return ids
}

/** Fold one `segment_<n>_<field>=<value>` line into its record. */
function applySegmentField(record: SegmentRecord, field: string, value: string): void {
  if (field === "id") record.id = value.trim()
  else if (field === "revision") record.revision = Number(value) || undefined
  else if (field === "script") record.script = value.trim()
  else if (field === "transcript") record.transcript = value.trim()
  else if (field === "stt") {
    const [verdict, similarity] = value.split(":", 2)
    record.sttVerdict = verdict || undefined
    const score = Number(similarity)
    if (Number.isFinite(score)) record.sttSimilarity = score
  }
}

function segmentsByOrdinal(tools: ToolPart[]): Map<number, SegmentRecord> {
  const byOrdinal = new Map<number, SegmentRecord>()
  for (const tool of tools) {
    if (tool.tool !== "video_project" || !tool.output) continue
    for (const line of tool.output.split("\n")) {
      const match = /^segment_(\d+)_(id|revision|script|transcript|stt)=(.*)$/.exec(line)
      if (!match) continue
      const ordinal = Number(match[1])
      const record = byOrdinal.get(ordinal) ?? { ordinal }
      applySegmentField(record, match[2], match[3])
      byOrdinal.set(ordinal, record)
    }
  }
  return byOrdinal
}

/** STT happens after the video file was attached.  Its later tool result is
 *  therefore the freshest QA source for both old and new transcript rows. */
function mergeTranscriptions(records: Map<string, SegmentRecord>, tools: ToolPart[]): void {
  for (const tool of tools) {
    if (tool.tool !== "video_transcribe") continue
    const segmentId = asString(tool.input?.segment_id) ?? outputValue(tool.output, "segment_id")
    if (!segmentId) continue
    const record = records.get(segmentId) ?? { id: segmentId, ordinal: 0 }
    record.transcript = outputValue(tool.output, "transcript") ?? record.transcript
    record.sttVerdict = outputValue(tool.output, "verdict") ?? record.sttVerdict
    const similarityValue = outputValue(tool.output, "similarity")
    const similarity = similarityValue == null ? Number.NaN : Number(similarityValue)
    if (Number.isFinite(similarity)) record.sttSimilarity = similarity
    records.set(segmentId, record)
  }
}

function parseSegmentRecords(tools: ToolPart[]): Map<string, SegmentRecord> {
  const records = new Map<string, SegmentRecord>()
  for (const record of segmentsByOrdinal(tools).values()) {
    records.set(`ordinal:${record.ordinal}`, record)
    if (record.id) records.set(record.id, record)
  }
  mergeTranscriptions(records, tools)
  return records
}

function outputValue(output: string | undefined, key: string): string | null {
  if (!output) return null
  const line = output.split("\n").find((item) => item.startsWith(`${key}=`))
  return line ? line.slice(key.length + 1).trim() || null : null
}

function inferKind(part: FilePart, tool: ToolPart | null): string {
  const declared = part.relation?.kind
  if (declared && declared !== "file") return declared
  if (tool?.tool === "computer") return "computer_screenshot"
  if (tool?.tool === "view_image") return "inspection_image"
  if (tool?.tool === "image_gen") return "generated_image"
  if (tool?.tool === "video_generate") return "video_segment"
  if (tool?.tool === "video_render") return "video_final"
  if (tool?.tool === "share_file") return "shared_file"
  if (part.transient) return "evidence"
  return "file"
}

function inferRole(part: FilePart, kind: string): ArtifactRole {
  if (part.relation?.role) return part.relation.role
  if (kind === "computer_screenshot" || kind === "inspection_image" || part.transient) {
    return "evidence"
  }
  if (kind === "video_segment") return "intermediate"
  if (kind === "video_final") return "final"
  return "result"
}

function sourceForFile(
  part: FilePart,
  precedingTools: ToolPart[],
  toolsById: Map<string, ToolPart>,
  toolsByAsset: Map<string, ToolPart>,
): ToolPart | null {
  const declared = asString(part.relation?.source_part_id)
  if (declared && toolsById.has(declared)) return toolsById.get(declared) ?? null
  if (part.asset_id && toolsByAsset.has(part.asset_id)) return toolsByAsset.get(part.asset_id) ?? null
  const namedOwner = precedingTools.find((tool) => part.path.includes(tool.id))
  if (namedOwner) return namedOwner
  return precedingTools[precedingTools.length - 1] ?? null
}

function segmentFor(
  part: FilePart,
  tool: ToolPart | null,
  records: Map<string, SegmentRecord>,
): SegmentRecord | null {
  const relationId = asString(part.relation?.metadata?.segment_id)
  const inputId = asString(tool?.input?.segment_id)
  const outputId = outputValue(tool?.output, "segment_id")
  const id = relationId ?? inputId ?? outputId
  if (id && records.has(id)) return records.get(id) ?? null
  const ordinal = part.relation?.ordinal
  return ordinal ? (records.get(`ordinal:${ordinal}`) ?? null) : null
}

function captionFor(
  part: FilePart,
  tool: ToolPart | null,
  kind: string,
  segment: SegmentRecord | null,
): string | null {
  const declared = asString(part.relation?.caption)
  if (declared) return declared
  if (kind === "generated_image") return asString(tool?.input?.prompt)
  if (kind === "video_segment") return segment?.script ?? null
  return null
}

function groupArtifacts(
  items: Array<Omit<ArtifactGroup, "kind" | "parts"> & { part: FilePart }>,
): ArtifactGroup[] {
  const groups = new Map<string, ArtifactGroup>()
  for (const item of items) {
    const existing = groups.get(item.id)
    if (existing) {
      existing.parts.push(item.part)
      existing.order = Math.min(existing.order, item.order)
      continue
    }
    groups.set(item.id, {
      kind: "artifact",
      id: item.id,
      order: item.order,
      artifactKind: item.artifactKind,
      role: item.role,
      label: item.label,
      caption: item.caption,
      ordinal: item.ordinal,
      revision: item.revision,
      metadata: item.metadata,
      sourceTool: item.sourceTool,
      parts: [item.part],
    })
  }
  return [...groups.values()].sort((a, b) => a.order - b.order)
}

function resultOrder(group: ArtifactGroup): number {
  if (group.role === "final") return 0
  if (group.role === "result") return 1
  return 2
}

/** The segment's QA fields land on the artifact only when the file itself did
 *  not already carry them. */
function artifactMetadata(part: FilePart, segment: SegmentRecord | null): Record<string, unknown> {
  const metadata: Record<string, unknown> = { ...(part.relation?.metadata ?? {}) }
  if (segment?.transcript && metadata.transcript == null) metadata.transcript = segment.transcript
  if (segment?.sttVerdict && metadata.stt_verdict == null) metadata.stt_verdict = segment.sttVerdict
  if (segment?.sttSimilarity != null && metadata.stt_similarity == null) {
    metadata.stt_similarity = segment.sttSimilarity
  }
  return metadata
}

/** What ties several files into one artifact: a declared group, the video
 *  segment or production they belong to, else the tool that produced them. */
function groupIdFor(
  part: FilePart,
  artifactKind: string,
  sourceTool: ToolPart | null,
  video: { segmentId: string | null; productionId: string | null },
): string {
  const declared = asString(part.relation?.group_id)
  if (declared) return declared
  const { segmentId, productionId } = video
  if (artifactKind === "video_segment" && segmentId) {
    return `video:${productionId ?? "unknown"}:segment:${segmentId}`
  }
  if (artifactKind === "video_final" && productionId) return `video:${productionId}:final`
  return sourceTool ? `tool:${sourceTool.id}` : `file:${part.id}`
}

interface ArtifactContext {
  toolsById: Map<string, ToolPart>
  toolsByAsset: Map<string, ToolPart>
  segmentRecords: Map<string, SegmentRecord>
}

type ArtifactEntry = Omit<ArtifactGroup, "kind" | "parts"> & { part: FilePart }

function buildArtifactEntry(
  part: FilePart,
  order: number,
  precedingTools: ToolPart[],
  ctx: ArtifactContext,
): ArtifactEntry {
  const sourceTool = sourceForFile(part, precedingTools, ctx.toolsById, ctx.toolsByAsset)
  const artifactKind = inferKind(part, sourceTool)
  const segment = segmentFor(part, sourceTool, ctx.segmentRecords)
  const metadata = artifactMetadata(part, segment)
  const segmentId =
    asString(metadata.segment_id) ??
    asString(sourceTool?.input?.segment_id) ??
    outputValue(sourceTool?.output, "segment_id")
  const productionId =
    asString(metadata.production_id) ??
    asString(sourceTool?.input?.production_id) ??
    outputValue(sourceTool?.output, "production_id")

  return {
    id: groupIdFor(part, artifactKind, sourceTool, { segmentId, productionId }),
    order,
    artifactKind,
    role: inferRole(part, artifactKind),
    label: asString(part.relation?.label) ?? sourceTool?.title ?? null,
    caption: captionFor(part, sourceTool, artifactKind, segment),
    ordinal: asNumber(part.relation?.ordinal) ?? segment?.ordinal ?? null,
    revision: asNumber(part.relation?.revision) ?? segment?.revision ?? null,
    metadata,
    sourceTool,
    part,
  }
}

export function buildAssistantContentView(
  messages: MessageWithParts[],
  streaming: boolean,
): AssistantContentView {
  const finalIndex = finalMessageIndex(messages, streaming)
  const finalParts = finalIndex >= 0 ? textParts(messages[finalIndex]) : []
  const finalText = finalParts
    .filter((part) => part.channel !== "commentary")
    .map((part) => part.text)
    .join("")
  const hasFinal = finalText.trim().length > 0

  const tools = messages.flatMap((message) =>
    message.parts.filter((part): part is ToolPart => part.type === "tool"),
  )
  const toolsById = new Map(tools.map((tool) => [tool.id, tool]))
  const toolsByAsset = new Map<string, ToolPart>()
  for (const tool of tools) {
    for (const assetId of metadataAssetIds(tool)) toolsByAsset.set(assetId, tool)
  }
  const segmentRecords = parseSegmentRecords(tools)

  const ctx: ArtifactContext = { toolsById, toolsByAsset, segmentRecords }
  const progress: WorkNarration[] = []
  const artifacts: ArtifactEntry[] = []
  let order = 0
  messages.forEach((message, messageIndex) => {
    const precedingTools: ToolPart[] = []
    const finalStep = messageIndex === finalIndex && !isToolStepFinish(message.finish)
    for (const part of message.parts) {
      order += 1
      if (part.type === "tool") precedingTools.push(part)
      if (part.type === "text" && !(finalStep && part.channel !== "commentary") && part.text.trim()) {
        progress.push({ kind: "narration", id: part.id, order, text: part.text })
      }
      if (part.type !== "file") continue
      artifacts.push(buildArtifactEntry(part, order, precedingTools, ctx))
    }
  })

  const groups = groupArtifacts(artifacts)
  const evidence = groups.filter((group) => group.role === "evidence")
  const results = groups
    .filter((group) => group.role !== "evidence" && group.role !== "input")
    .sort((a, b) => {
      const role = resultOrder(a) - resultOrder(b)
      if (role !== 0) return role
      if (a.artifactKind === "video_segment" && b.artifactKind === "video_segment") {
        return (a.ordinal ?? Number.MAX_SAFE_INTEGER) - (b.ordinal ?? Number.MAX_SAFE_INTEGER)
      }
      return a.order - b.order
    })

  // Computer-use produces one screenshot per action.  Keep checkpoints in
  // the work log, but surface the last frame as final verification only when
  // the turn has no richer deliverable of its own.
  const computerEvidence = evidence.filter((group) => group.artifactKind === "computer_screenshot")
  const verification = hasFinal && results.length === 0 ? (computerEvidence.at(-1) ?? null) : null
  const workEvidence = verification ? evidence.filter((group) => group.id !== verification.id) : evidence
  const workEvents: WorkEvent[] = [...progress, ...workEvidence].sort((a, b) => a.order - b.order)
  const hasWork = progress.length > 0 || tools.length > 0 || groups.length > 0

  return {
    finalText,
    finalMessageId: finalIndex >= 0 ? (messages[finalIndex]?.id ?? null) : null,
    hasFinal,
    progress,
    workEvents,
    resultGroups: results,
    verification,
    incomplete: !streaming && !hasFinal && hasWork,
  }
}
