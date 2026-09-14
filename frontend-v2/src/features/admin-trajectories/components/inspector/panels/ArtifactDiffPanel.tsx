import { useTranslation } from "react-i18next"
import { fileDiffOf, fileRevisions, type FileRevision } from "../../../utils/artifact"
import type { FieldState } from "../../../utils/availability"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { InstantValue } from "../InstantValue"
import { UnifiedDiffView } from "../UnifiedDiffView"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

interface DiffBodyProps {
  diff: FieldState
  beforeMissing: boolean
}

interface RevisionItemProps {
  revision: FileRevision
  index: number
}

function DiffBody({ diff, beforeMissing }: DiffBodyProps) {
  const { t } = useTranslation(NS)
  if (diff.state === "available" && typeof diff.value === "string")
    return <UnifiedDiffView diff={diff.value} />
  return (
    <div className="flex flex-col gap-1">
      <ValueView field={diff} />
      {beforeMissing && <p className="text-n600 text-xs">{t("artifact.noDiff")}</p>}
    </div>
  )
}

function RevisionItem({ revision, index }: RevisionItemProps) {
  const { t } = useTranslation(NS)
  return (
    <details className="border-hair rounded-lg border px-3 py-2" data-testid="trajectory-file-revision">
      <summary className="text-n700 flex cursor-pointer flex-wrap items-center gap-x-3 text-xs">
        <span className="text-ink font-medium">{t("artifact.revision", { index: index + 1 })}</span>
        <span className="font-mono">{t("events.seq", { seq: revision.seq })}</span>
        {revision.operation && <span>{revision.operation}</span>}
        <InstantValue iso={revision.occurredAt} />
      </summary>
      <div className="mt-2">
        <DiffBody diff={revision.diff} beforeMissing={revision.before.field.state === "not_recorded"} />
      </div>
    </details>
  )
}

/** The captured unified diff of the latest write and of every earlier recorded write up to this position. */
export function ArtifactDiffPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const file = fileDiffOf(record)
  if (!file) return <AvailabilityNote state="not_recorded" />
  const revisions = record.events ? fileRevisions(record, record.events) : []
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-artifact-diff">
      <Section
        title={t("artifact.diff")}
        actions={
          file.diff.state === "available" && typeof file.diff.value === "string" ? (
            <ContentActions value={file.diff.value} name={`${record.record_id}-diff`} format="text" />
          ) : undefined
        }
      >
        <DiffBody diff={file.diff} beforeMissing={file.before.field.state === "not_recorded"} />
      </Section>
      {revisions.length > 1 && (
        <Section title={t("artifact.revisions", { count: revisions.length })}>
          <div className="flex flex-col gap-2">
            {revisions.map((revision, index) => (
              <RevisionItem key={revision.seq} revision={revision} index={index} />
            ))}
          </div>
        </Section>
      )}
    </div>
  )
}
