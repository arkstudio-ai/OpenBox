import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CALL_DETAILS, type AssistantProfile } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
import { Choices, Switch } from "./controls"
import {
  useAssistantVoices,
  useChooseVoice,
  voiceSampleUrl,
  type AssistantVoice,
} from "@/features/settings/api/voice"

type Group = "zhFemale" | "zhMale" | "en"
const GROUPS: Group[] = ["zhFemale", "zhMale", "en"]

function groupOf(voice: AssistantVoice): Group {
  if (voice.lang === "en") return "en"
  return voice.gender === "male" ? "zhMale" : "zhFemale"
}

/** One selectable voice: name, its id, one line about it, and a preview button beside it. */
function VoiceCard({
  voice,
  active,
  isDefault,
  playing,
  disabled,
  onPick,
  onPreview,
}: {
  voice: AssistantVoice
  active: boolean
  isDefault: boolean
  playing: boolean
  disabled: boolean
  onPick: () => void
  onPreview: () => void
}) {
  const { t, i18n } = useTranslation("settings")
  const english = i18n.language?.startsWith("en")
  return (
    <div
      className={cn(
        "bg-card flex items-start gap-2 rounded-lg border px-4 py-3",
        active ? "border-ink" : "border-hair",
      )}
    >
      <button
        type="button"
        onClick={onPick}
        aria-pressed={active}
        disabled={disabled}
        className="flex min-w-0 flex-1 flex-col gap-1 text-start"
      >
        <span className="flex flex-wrap items-center gap-2 text-base">
          {english ? voice.id : voice.name}
          <span className="text-n600 text-xs">{english ? voice.name : voice.id}</span>
          {isDefault && (
            <span className="bg-n200 text-2xs text-n700 rounded-full px-2 py-0.5 font-medium">
              {t("voice.defaultTag")}
            </span>
          )}
          {active && (
            <span className="border-ink text-ink text-2xs rounded-full border px-2 py-0.5 font-medium">
              {t("voice.current")}
            </span>
          )}
        </span>
        <span className="text-n600 text-xs text-pretty">
          {english ? voice.description_en : voice.description}
        </span>
      </button>
      <button
        type="button"
        onClick={onPreview}
        disabled={disabled}
        aria-label={t("voice.preview", { name: english ? voice.id : voice.name })}
        className="text-n700 hover:bg-hairsoft flex-none rounded-md px-2 py-1 text-xs"
      >
        {playing ? t("voice.stop") : t("voice.listen")}
      </button>
    </div>
  )
}

type CallHabit = Pick<AssistantProfile, "call_recap" | "call_reports" | "call_detail">

/** How calls go: whether the greeting brings up the last call, whether finished work nobody asked
 *  about on the call is told, and how much an answer says. Saved at once; a call in progress
 *  follows it from its next answer (the server rebuilds the call's instructions). */
function CallHabits() {
  const { t } = useTranslation("settings")
  const profile = useAppearanceStore((s) => s.assistant)
  const setAssistantProfile = useAppearanceStore((s) => s.setAssistantProfile)
  const [saving, setSaving] = useState(false)
  const save = async (patch: Partial<CallHabit>) => {
    setSaving(true)
    try {
      await setAssistantProfile(patch)
      toast("success", t("voice.call.saved"))
    } catch {
      toast("error", t("voice.call.saveFailed"))
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="flex flex-col gap-2.5">
      <span className="text-n600 text-xs">{t("voice.call.title")}</span>
      <div className="grid gap-2.5 md:grid-cols-2">
        <Switch
          on={profile.call_recap}
          label={t("voice.call.recap")}
          hint={t("voice.call.recapHint")}
          disabled={saving}
          onToggle={() => void save({ call_recap: !profile.call_recap })}
        />
        <Switch
          on={profile.call_reports}
          label={t("voice.call.reports")}
          hint={t("voice.call.reportsHint")}
          disabled={saving}
          onToggle={() => void save({ call_reports: !profile.call_reports })}
        />
      </div>
      <Choices
        label={t("voice.call.detail")}
        options={CALL_DETAILS}
        value={profile.call_detail}
        text={(detail) => t(`voice.call.detailOption.${detail}`)}
        disabled={saving}
        onPick={(call_detail) => call_detail !== profile.call_detail && void save({ call_detail })}
      />
    </div>
  )
}

export function VoicePage() {
  const { t, i18n } = useTranslation("settings")
  const voices = useAssistantVoices()
  const choose = useChooseVoice()
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)

  useEffect(
    () => () => {
      audio.current?.pause()
      audio.current = null
    },
    [],
  )

  const list = voices.data?.voices ?? []
  const selected = voices.data?.selected
  const model = voices.data?.model
  const english = i18n.language?.startsWith("en")

  const preview = (id: string) => {
    audio.current?.pause()
    if (playing === id) {
      audio.current = null
      setPlaying(null)
      return
    }
    const player = new Audio(voiceSampleUrl(id, model))
    audio.current = player
    player.onended = () => {
      if (audio.current === player) setPlaying(null)
    }
    setPlaying(id)
    player.play().catch(() => {
      if (audio.current !== player) return
      setPlaying(null)
      toast("error", t("voice.previewFailed"))
    })
  }

  const pick = (voice: AssistantVoice) => {
    if (voice.id === selected || choose.isPending) return
    choose.mutate(
      { voice: voice.id, model },
      {
        onSuccess: () => toast("success", t("voice.saved", { name: english ? voice.id : voice.name })),
        onError: () => toast("error", t("voice.saveFailed")),
      },
    )
  }

  const pickModel = (id: string) => {
    if (id === model || choose.isPending) return
    audio.current?.pause()
    audio.current = null
    setPlaying(null)
    choose.mutate(
      { model: id },
      {
        onSuccess: () => toast("success", t("voice.model.saved")),
        onError: () => toast("error", t("voice.saveFailed")),
      },
    )
  }

  if (voices.isError)
    return (
      <div className="flex flex-col gap-6">
        <p className="text-n600 text-sm">{t("voice.loadFailed")}</p>
        <CallHabits />
      </div>
    )

  return (
    <div className="flex flex-col gap-6">
      {!!voices.data?.models?.length && (
        <div className="flex flex-col gap-2.5">
          <span className="text-n600 text-xs">{t("voice.model.title")}</span>
          <div className="grid grid-cols-2 gap-2.5" role="group" aria-label={t("voice.model.title")}>
            {voices.data.models.map((option) => (
              <button
                key={option.id}
                type="button"
                disabled={choose.isPending}
                aria-pressed={model === option.id}
                onClick={() => pickModel(option.id)}
                className={cn(
                  "bg-card flex flex-col gap-1 rounded-lg border px-4 py-3 text-start",
                  model === option.id ? "border-ink" : "border-hair",
                )}
              >
                <span className="flex flex-wrap items-center gap-2 text-base">
                  {t(`voice.model.${option.tier}`)}
                  {option.id === voices.data?.default_model && (
                    <span className="bg-n200 text-n700 text-2xs rounded-full px-2 py-0.5">
                      {t("voice.model.defaultTag")}
                    </span>
                  )}
                </span>
                <span className="text-n600 text-xs">{option.name}</span>
              </button>
            ))}
          </div>
          <p className="text-n600 text-xs text-pretty">{t("voice.model.hint")}</p>
        </div>
      )}
      {GROUPS.map((group) => {
        const items = list.filter((voice) => groupOf(voice) === group)
        if (!items.length) return null
        return (
          <div key={group} className="flex flex-col gap-2.5">
            <span className="text-n600 text-xs">{t(`voice.group.${group}`)}</span>
            <div className="grid gap-2.5 md:grid-cols-2">
              {items.map((voice) => (
                <VoiceCard
                  key={voice.id}
                  voice={voice}
                  active={voice.id === selected}
                  isDefault={voice.id === voices.data?.default}
                  playing={playing === voice.id}
                  disabled={choose.isPending}
                  onPick={() => pick(voice)}
                  onPreview={() => preview(voice.id)}
                />
              ))}
            </div>
          </div>
        )
      })}
      <p className="text-n600 text-xs text-pretty">{t("voice.note")}</p>
      <CallHabits />
    </div>
  )
}
