import { useRef } from "react"
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
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
import { Switch } from "./controls"

interface Props {
  compact?: boolean
}
export function CompanionAppearanceSettings({ compact = false }: Props) {
  const { t } = useTranslation("companion")
  const query = useCompanionAppearance()
  const save = useSaveCompanionAppearance()
  const upload = useUploadCompanionImage()
  const input = useRef<HTMLInputElement>(null)
  const appearance = query.data ?? DEFAULT_COMPANION
  const busy = save.isPending || upload.isPending || !query.data
  const choices: CompanionPreset[] = [
    ...COMPANION_PRESETS,
    ...(appearance.has_image ? ["custom" as const] : []),
  ]
  const feedback = {
    onSuccess: () => toast("success", t("settings.saved")),
    onError: () => toast("error", t("settings.saveFailed")),
  }

  return (
    <section className={compact ? "space-y-3" : "space-y-4"} aria-label={t("appearance.title")}>
      <div>
        <h3 className="text-ink text-sm font-medium">{t("appearance.title")}</h3>
        {!compact && <p className="text-n700 mt-1 text-xs leading-5">{t("appearance.hint")}</p>}
      </div>
      {query.isError && (
        <div role="alert" className="text-danger-ink text-xs">
          {t("settings.loadFailed")}
          <button type="button" onClick={() => void query.refetch()} className="ms-2 underline">
            {t("retry")}
          </button>
        </div>
      )}
      <div className="flex flex-wrap gap-2" role="group" aria-label={t("appearance.title")}>
        {choices.map((preset) => (
          <button
            key={preset}
            type="button"
            disabled={busy}
            aria-pressed={appearance.preset === preset}
            onClick={() => save.mutate({ preset }, feedback)}
            className={cn(
              "flex flex-col items-center gap-1 rounded-2xl border px-2 py-2.5 text-xs transition-colors disabled:opacity-50",
              compact ? "w-18" : "w-20",
              appearance.preset === preset
                ? "border-ink bg-surface"
                : "border-hair bg-card hover:bg-hairsoft",
            )}
          >
            <CompanionAvatar
              appearance={{ ...appearance, preset }}
              className={compact ? "size-10" : "size-12"}
            />
            {t(`appearance.${preset}`)}
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
