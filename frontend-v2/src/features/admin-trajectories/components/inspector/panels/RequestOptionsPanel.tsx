import { useTranslation } from "react-i18next"
import { REQUEST_OPTION_KEYS } from "../../../constants/inspector"
import { isPlainObject, type PlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { NS, type PanelProps } from "../types"

const DISPATCH_KEYS = ["provider", "model", "purpose", "attempt", "capture_level"] as const

function pick(source: PlainObject | null): Array<[string, unknown]> {
  if (!source) return []
  return REQUEST_OPTION_KEYS.filter((key) => key in source).map((key) => [key, source[key]])
}

/** Options that applied to this dispatch, exactly as captured; nothing is filled in from defaults. */
export function RequestOptionsPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const data = record.data ?? {}
  const input = isPlainObject(data.input) ? data.input : null
  const extra = input && isPlainObject(input.extra_body) ? input.extra_body : null
  const options = pick(input)
  const extraOptions = pick(extra)
  const dispatch = DISPATCH_KEYS.filter((key) => key in data)
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-request-options">
      <Section title={t("options.dispatch")}>
        {dispatch.length ? (
          <FieldList>
            {dispatch.map((key) => (
              <Field key={key} label={key}>
                <InlineValue value={data[key]} />
              </Field>
            ))}
          </FieldList>
        ) : (
          <AvailabilityNote state="not_recorded" />
        )}
      </Section>
      <Section title={t("options.parameters")}>
        {options.length ? (
          <FieldList>
            {options.map(([key, value]) => (
              <Field key={key} label={key}>
                <InlineValue value={value} />
              </Field>
            ))}
          </FieldList>
        ) : (
          <AvailabilityNote state="not_recorded" />
        )}
      </Section>
      {extraOptions.length > 0 && (
        <Section title={t("options.providerExtras")}>
          <FieldList>
            {extraOptions.map(([key, value]) => (
              <Field key={key} label={key}>
                <InlineValue value={value} />
              </Field>
            ))}
          </FieldList>
        </Section>
      )}
    </div>
  )
}
