import { useRef, useState } from "react"
import { Upload } from "lucide-react"
import { useTranslation } from "react-i18next"
import { CompanionAvatar } from "@/shared/appearance/CompanionAvatar"
import {
  COMPANION_PRESETS,
  DEFAULT_COMPANION,
  useCompanionAppearance,
  useSaveCompanionAppearance,
  useUploadCompanionImage,
  type CompanionPreset,
} from "@/shared/appearance/companion"
import { resolveCompanionPreset } from "@/shared/appearance/companion-presets"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
import { Switch } from "./controls"

interface Props {
  compact?: boolean
}
const CATEGORIES = ["all", "female", "male", "pets"] as const
export function CompanionAppearanceSettings({ compact = false }: Props) {
  const { t } = useTranslation("companion")
  const query = useCompanionAppearance()
  const save = useSaveCompanionAppearance()
  const upload = useUploadCompanionImage()
  const input = useRef<HTMLInputElement>(null)
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>("all")
  const appearance = query.data ?? DEFAULT_COMPANION
  const selected = resolveCompanionPreset(appearance.preset)
  const busy = save.isPending || upload.isPending || !query.data
  const choices: CompanionPreset[] = [
    ...COMPANION_PRESETS,
    ...(appearance.has_image ? ["custom" as const] : []),
  ]
  const details = new Map(appearance.presets?.map((option) => [option.id, option]))
  const visible = choices.filter(
    (preset) => category === "all" || (preset !== "custom" && details.get(preset)?.category === category),
  )
  const feedback = {
    onSuccess: () => toast("success", t("settings.saved")),
    onError: () => toast("error", t("settings.saveFailed")),
  }

  return (
    <section className={compact ? "space-y-3" : "space-y-4"} aria-label={t("appearance.title")}>
      <div>
        <h3 className="text-ink text-sm font-medium">{t("appearance.title")}</h3>
        {!compact && <p className="text-n700 mt-1 text-xs leading-5">{t("appearance.hint")}</p>}
        <p className="text-n700 mt-1 text-xs leading-5">{t("appearance.voiceHint")}</p>
      </div>
      {query.isError && (
        <div role="alert" className="text-danger-ink text-xs">
          {t("settings.loadFailed")}
          <button type="button" onClick={() => void query.refetch()} className="ms-2 underline">
            {t("retry")}
          </button>
        </div>
      )}
      <div className="border-hair bg-surface flex items-center gap-3 rounded-2xl border p-3">
        <CompanionAvatar appearance={appearance} className={compact ? "size-14" : "size-18"} />
        <p className="text-n700 text-sm">{t("appearance.selected", { name: t(`appearance.${selected}`) })}</p>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t("appearance.categories")}>
        {CATEGORIES.map((item) => (
          <button
            key={item}
            type="button"
            disabled={item !== "all" && !appearance.presets}
            aria-pressed={category === item}
            onClick={() => setCategory(item)}
            className={cn(
              "min-h-9 rounded-full border px-3 text-xs transition-colors",
              category === item ? "border-ink bg-ink text-bg" : "border-hair hover:bg-hairsoft text-n700",
            )}
          >
            {t(`appearance.category.${item}`)}
          </button>
        ))}
      </div>
      <div
        className={cn(
          "scr grid grid-cols-[repeat(auto-fill,minmax(88px,1fr))] gap-2 overflow-y-auto overscroll-contain p-1",
          compact ? "max-h-72" : "max-h-96",
        )}
        role="group"
        aria-label={t("appearance.title")}
      >
        {visible.map((preset) => (
          <button
            key={preset}
            type="button"
            disabled={busy}
            aria-label={t(`appearance.${preset}`)}
            aria-pressed={selected === preset}
            onClick={() => save.mutate({ preset }, feedback)}
            className={cn(
              "flex min-w-0 flex-col items-center gap-1 rounded-2xl border px-2 py-2.5 text-xs transition-colors disabled:opacity-50",
              selected === preset ? "border-ink bg-surface" : "border-hair bg-card hover:bg-hairsoft",
            )}
          >
            <CompanionAvatar
              appearance={{ ...appearance, preset }}
              className={compact ? "size-14" : "size-18"}
              loading="lazy"
            />
            <span className="flex min-h-8 items-center text-center leading-4">
              {t(`appearance.${preset}`)}
            </span>
            {(preset === "custom" || details.has(preset)) && (
              <span className="text-n700 text-2xs">
                {t(
                  `appearance.voiceGender.${preset === "custom" ? "female" : details.get(preset)!.voice_gender}`,
                )}
              </span>
            )}
          </button>
        ))}
      </div>
      <div>
        <input
          ref={input}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          className="hidden"
          aria-label={t("appearance.upload")}
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ""
            if (file)
              upload.mutate(file, {
                onSuccess: feedback.onSuccess,
                onError: (error) =>
                  toast(
                    "error",
                    t(error.message === "portraitInvalid" ? "appearance.invalid" : "appearance.failed"),
                  ),
              })
          }}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => input.current?.click()}
          className="border-hair hover:bg-hairsoft flex min-h-11 items-center gap-2 rounded-xl border px-3 text-sm disabled:opacity-50"
        >
          <Upload size={16} aria-hidden />
          {t(upload.isPending ? "appearance.uploading" : "appearance.upload")}
        </button>
        <p className="text-n700 mt-2 text-xs leading-5">
          {t(compact ? "appearance.uploadHintShort" : "appearance.uploadHint")}
        </p>
      </div>
      {!compact && (
        <Switch
          on={appearance.enabled}
          label={t("appearance.enabled")}
          hint={t("appearance.enabledHint")}
          disabled={busy}
          onToggle={() => save.mutate({ enabled: !appearance.enabled }, feedback)}
        />
      )}
    </section>
  )
}
