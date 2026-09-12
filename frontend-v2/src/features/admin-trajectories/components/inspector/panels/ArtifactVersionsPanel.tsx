import { useTranslation } from "react-i18next"
import { formatBytes } from "@/shared/lib/format"
import { ARTIFACT_FACT_KEYS, FIELD_LABELS } from "../../../constants/inspector"
import { CAPTURE_LEVEL_LABELS, labelKey } from "../../../constants/labels"
import { fileDiffOf, type FileVersion } from "../../../utils/artifact"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { CopyButton } from "../CopyButton"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { TextBlock } from "../TextBlock"
import { NS, type PanelProps } from "../types"

interface VersionCardProps {
  title: string
  version: FileVersion
  name: string
  testId: string
}

function VersionCard({ title, version, name, testId }: VersionCardProps) {
  const { t } = useTranslation(NS)
  const { field } = version
  return (
    <Section
      title={title}
      actions={
        version.text !== null ? <ContentActions value={version.text} name={name} format="text" /> : undefined
      }
    >
      <div className="flex flex-col gap-2" data-testid={testId} data-availability={field.state}>
        {field.state === "available" && (
          <FieldList>
            {version.sha256 && (
              <Field label={t("field.sha256")}>
                <span className="inline-flex items-center gap-1 font-mono break-all">
                  {version.sha256}
                  <CopyButton text={version.sha256} label={t("common.copyValue")} className="size-5" />
                </span>
              </Field>
            )}
            {version.sizeBytes !== null && (
              <Field label={t("field.size")}>{formatBytes(version.sizeBytes)}</Field>
            )}
            {version.source && (
              <Field label={t("artifact.source")}>
                {t(labelKey(CAPTURE_LEVEL_LABELS, version.source, "captureLevel.other"), {
                  value: version.source,
                })}
              </Field>
            )}
            {version.redacted && <Field label={t("artifact.redacted")}>{t("artifact.redactedNote")}</Field>}
          </FieldList>
        )}
        {field.state === "available" &&
          (version.text !== null ? (
            <TextBlock text={version.text} />
          ) : (
            <AvailabilityNote state="not_recorded" />
          ))}
        {field.state === "absent" && <p className="text-n600 text-xs">{t("artifact.absent")}</p>}
        {field.state === "deleted" && <AvailabilityNote state="deleted" reason={field.reason} />}
        {field.state !== "available" &&
          field.state !== "absent" &&
          field.state !== "deleted" &&
          field.state !== "payload" && <AvailabilityNote state={field.state} />}
      </div>
    </Section>
  )
}

/** Retained versions: the file before and after the executor's write, with hashes and sizes as captured. */
export function ArtifactVersionsPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const file = fileDiffOf(record)
  if (!file) {
    const data = record.data ?? {}
    const keys = ARTIFACT_FACT_KEYS.filter((key) => key in data)
    if (!keys.length) return <AvailabilityNote state="not_recorded" />
    return (
      <FieldList>
        {keys.map((key) => (
          <Field key={key} label={t(labelKey(FIELD_LABELS, key, "field.other"), { value: key })}>
            <InlineValue value={data[key]} />
          </Field>
        ))}
      </FieldList>
    )
  }
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-artifact-versions">
      <FieldList>
        {file.path && <Field label={t("field.path")}>{file.path}</Field>}
        {file.operation && <Field label={t("field.operation")}>{file.operation}</Field>}
        {file.captureLevel && (
          <Field label={t("field.captureLevel")}>
            {t(labelKey(CAPTURE_LEVEL_LABELS, file.captureLevel, "captureLevel.other"), {
              value: file.captureLevel,
            })}
          </Field>
        )}
      </FieldList>
      <VersionCard
        title={t("artifact.before")}
        version={file.before}
        name={`${record.record_id}-before`}
        testId="trajectory-version-before"
      />
      <VersionCard
        title={t("artifact.after")}
        version={file.after}
        name={`${record.record_id}-after`}
        testId="trajectory-version-after"
      />
    </div>
  )
}
