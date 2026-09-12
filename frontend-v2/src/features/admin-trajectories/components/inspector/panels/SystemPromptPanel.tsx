import { useTranslation } from "react-i18next"
import { CAPTURE_LEVEL_LABELS, labelKey } from "../../../constants/labels"
import { fieldState } from "../../../utils/availability"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { MessageView } from "../MessageView"
import { TextBlock } from "../TextBlock"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

/** The effective system content at this state, taken from the request that carried it. */
export function SystemPromptPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const system = fieldState(record, "system")
  const capture = typeof record.data?.capture_level === "string" ? record.data.capture_level : null
  return (
    <div className="flex flex-col gap-3" data-testid="trajectory-system-prompt">
      {capture && (
        <p className="text-n600 text-xs">
          {t(labelKey(CAPTURE_LEVEL_LABELS, capture, "captureLevel.other"), { value: capture })}
        </p>
      )}
      <Section
        title={t("system.prompt")}
        actions={
          system.state === "available" ? (
            <ContentActions
              value={system.value}
              name={`${record.record_id}-system`}
              format={typeof system.value === "string" ? "text" : "json"}
            />
          ) : undefined
        }
      >
        {system.state === "available" && typeof system.value === "string" && (
          <TextBlock text={system.value} mono={false} />
        )}
        {system.state === "available" && Array.isArray(system.value) && (
          <div className="flex flex-col gap-2">
            {system.value.map((message, index) => (
              <MessageView key={index} message={message} index={index} />
            ))}
          </div>
        )}
        {(system.state !== "available" ||
          (typeof system.value !== "string" && !Array.isArray(system.value))) && <ValueView field={system} />}
      </Section>
    </div>
  )
}
