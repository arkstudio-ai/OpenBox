import type { ChangeEvent } from "react"
import { useTranslation } from "react-i18next"
import type { SkillDisplay } from "@/shared/lib/skill-display"

const DISPLAY_LOCALES = ["zh-CN", "en-US"] as const
const DISPLAY_KEYS = ["display_name", "display_description"] as const
const INPUT_CLASS =
  "border-hair bg-canvas text-ink focus:border-accent mt-1 w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none"

export function SkillDisplayFields({
  value,
  onChange,
  disabled = false,
}: {
  value: SkillDisplay
  onChange: (value: SkillDisplay) => void
  disabled?: boolean
}) {
  const { t } = useTranslation("skills")
  return (
    <fieldset disabled={disabled} className="border-hair mt-4 space-y-3 border-t pt-3 disabled:opacity-50">
      <legend className="text-ink text-xs font-medium">{t("display.title")}</legend>
      <p className="text-n600 text-xs leading-5">{t("display.hint")}</p>
      <div className="grid grid-cols-2 gap-3">
        {DISPLAY_LOCALES.map((locale) => (
          <div key={locale} className="space-y-2">
            {DISPLAY_KEYS.map((key) => {
              const label = t(`display.${key}.${locale}`)
              const props = {
                value: value[key]?.[locale] ?? "",
                onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
                  onChange({ ...value, [key]: { ...value[key], [locale]: event.target.value } }),
                maxLength: key === "display_name" ? 120 : 1000,
                className: INPUT_CLASS,
              }
              return (
                <label key={key} className="text-n600 block text-xs">
                  {label}
                  {key === "display_name" ? <input {...props} /> : <textarea {...props} rows={3} />}
                </label>
              )
            })}
          </div>
        ))}
      </div>
    </fieldset>
  )
}
