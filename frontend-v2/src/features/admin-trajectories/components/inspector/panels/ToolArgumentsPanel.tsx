import { useTranslation } from "react-i18next"
import { fieldState, isClosed } from "../../../utils/availability"
import { stableText } from "../../../utils/diff"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { DiffView } from "../DiffView"
import { Section } from "../Field"
import { RecordLink } from "../RecordLink"
import { TextBlock } from "../TextBlock"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

/**
 * The three argument objects kept apart: what the model streamed, what parsed
 * from it, and what the executor actually ran after any approval revised it.
 * Arguments still streaming stay a raw string — never an empty object.
 */
export function ToolArgumentsPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const data = record.data ?? {}
  const open = !isClosed(record)
  const raw = typeof data.arguments_raw === "string" ? data.arguments_raw : null
  const parsedMissing = !("requested_arguments" in data) || data.requested_arguments === null
  const requested = fieldState(record, "requested_arguments", true)
  const effective = fieldState(record, "effective_arguments", true)
  const notExecuted = !open && !record.started_at
  const revised =
    requested.state === "available" &&
    effective.state === "available" &&
    stableText(requested.value) !== stableText(effective.value)
  const permissions = Object.values(records).filter(
    (item) => item.kind === "permission" && record.call_id && item.call_id === record.call_id,
  )
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-tool-arguments">
      <Section
        title={t("tool.argumentsRaw")}
        actions={
          raw ? <ContentActions value={raw} name={`${record.record_id}-raw`} format="text" /> : undefined
        }
      >
        {open && parsedMissing && raw !== null && (
          <p className="text-a700 text-xs">{t("tool.argumentsGenerating")}</p>
        )}
        {raw !== null ? (
          <TextBlock text={raw} />
        ) : (
          <AvailabilityNote state={open ? "pending" : "not_recorded"} />
        )}
      </Section>
      <Section
        title={t("tool.requestedArguments")}
        actions={
          requested.state === "available" ? (
            <ContentActions value={requested.value} name={`${record.record_id}-requested`} format="json" />
          ) : undefined
        }
      >
        {!open && "requested_arguments" in data && data.requested_arguments === null ? (
          <p className="text-dangerink text-xs">{t("tool.argumentsUnparsed")}</p>
        ) : (
          <ValueView field={requested} />
        )}
      </Section>
      <Section
        title={t("tool.effectiveArguments")}
        actions={
          effective.state === "available" ? (
            <ContentActions value={effective.value} name={`${record.record_id}-effective`} format="json" />
          ) : undefined
        }
      >
        {notExecuted && effective.state !== "available" ? (
          <p className="text-n600 text-xs">{t("tool.notExecutedArguments")}</p>
        ) : (
          <ValueView field={effective} />
        )}
      </Section>
      {revised && requested.state === "available" && effective.state === "available" && (
        <Section title={t("tool.revision")}>
          <DiffView before={stableText(requested.value)} after={stableText(effective.value)} />
        </Section>
      )}
      {permissions.length > 0 && (
        <Section title={t("tool.approvals")}>
          <ul className="flex flex-col gap-1">
            {permissions.map((permission) => (
              <li key={permission.record_id}>
                <RecordLink recordId={permission.record_id} />
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
