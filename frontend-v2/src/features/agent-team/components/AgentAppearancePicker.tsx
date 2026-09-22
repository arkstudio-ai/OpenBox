import { useId, type ReactNode } from "react"
import { Check } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { AgentSpec } from "../types"
import { AGENT_COLORS, AGENT_ICONS, agentColorOption, agentIconOption } from "../lib/appearance"
import { AgentAvatar } from "./AgentAvatar"

function AppearanceChoice({
  name,
  value,
  label,
  checked,
  onChange,
  children,
}: {
  name: string
  value: string
  label: string
  checked: boolean
  onChange: () => void
  children: ReactNode
}) {
  return (
    <label className="min-w-0 cursor-pointer">
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={onChange}
        className="peer sr-only"
      />
      <span className="border-hair text-n700 hover:bg-hairsoft peer-checked:border-ink peer-checked:bg-hairsoft peer-checked:text-ink peer-focus-visible:outline-ink relative flex h-full min-h-16 flex-col items-center justify-center gap-1.5 rounded-lg border px-1 py-2 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2">
        <span aria-hidden className="flex h-6 items-center justify-center">
          {children}
        </span>
        <span className="text-2xs text-center leading-tight">{label}</span>
        <Check aria-hidden className={cn("absolute end-1 top-1 size-2.5", !checked && "invisible")} />
      </span>
    </label>
  )
}

export function AgentAppearancePicker({
  display,
  name,
  onChange,
}: {
  display: AgentSpec["display"]
  name: string
  onChange: (display: AgentSpec["display"]) => void
}) {
  const { t } = useTranslation("teams")
  const id = useId()
  const icon = agentIconOption(display.icon)
  const color = agentColorOption(display.color)
  const iconLabel = icon ? t(`appearance.icons.${icon.value}`) : t("appearance.currentIcon")
  const colorLabel = color ? t(`appearance.colors.${color.value}`) : t("appearance.currentColor")
  const previewName = name.trim() || t("appearance.unnamedAgent")

  return (
    <div className="space-y-4">
      <div
        role="img"
        aria-label={t("appearance.previewLabel", { name: previewName, icon: iconLabel, color: colorLabel })}
        className="bg-bg flex items-center gap-3 rounded-lg p-3"
      >
        <AgentAvatar display={display} className="size-11" />
        <div aria-hidden className="min-w-0">
          <p className="truncate text-sm font-medium">{previewName}</p>
          <p className="text-n600 text-xs">{t("appearance.preview")}</p>
        </div>
      </div>
      <fieldset className="min-w-0">
        <legend className="text-n600 mb-2 text-xs">{t("icon")}</legend>
        <div className="grid grid-cols-6 gap-1.5">
          {AGENT_ICONS.map(({ value, Icon }) => (
            <AppearanceChoice
              key={value}
              name={`${id}-icon`}
              value={value}
              label={t(`appearance.icons.${value}`)}
              checked={icon?.value === value}
              onChange={() => onChange({ ...display, icon: value })}
            >
              <Icon className="size-5" />
            </AppearanceChoice>
          ))}
          {!icon && (
            <AppearanceChoice
              name={`${id}-icon`}
              value={display.icon}
              label={iconLabel}
              checked
              onChange={() => {}}
            >
              <AgentAvatar display={display} className="size-6 rounded-md border-0" />
            </AppearanceChoice>
          )}
        </div>
      </fieldset>
      <fieldset className="min-w-0">
        <legend className="text-n600 mb-2 text-xs">{t("color")}</legend>
        <div className="grid grid-cols-6 gap-1.5">
          {AGENT_COLORS.map(({ value, swatch }) => (
            <AppearanceChoice
              key={value}
              name={`${id}-color`}
              value={value}
              label={t(`appearance.colors.${value}`)}
              checked={color?.value === value}
              onChange={() => onChange({ ...display, color: value })}
            >
              <span className={cn("size-5 rounded-full", swatch)} />
            </AppearanceChoice>
          ))}
          {!color && (
            <AppearanceChoice
              name={`${id}-color`}
              value={display.color}
              label={colorLabel}
              checked
              onChange={() => {}}
            >
              <span className="bg-hairsoft border-hair size-5 rounded-full border" />
            </AppearanceChoice>
          )}
        </div>
      </fieldset>
    </div>
  )
}
