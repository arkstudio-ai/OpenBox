import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
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
  onPick,
  onPreview,
}: {
  voice: AssistantVoice
  active: boolean
  isDefault: boolean
  playing: boolean
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
        aria-label={t("voice.preview", { name: english ? voice.id : voice.name })}
        className="text-n700 hover:bg-hairsoft flex-none rounded-md px-2 py-1 text-xs"
      >
        {playing ? t("voice.stop") : t("voice.listen")}
      </button>
    </div>
  )
}

export function VoicePage() {
  const { t, i18n } = useTranslation("settings")
  const voices = useAssistantVoices()
  const choose = useChooseVoice()
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)

  useEffect(() => () => audio.current?.pause(), [])

  const list = voices.data?.voices ?? []
  const selected = voices.data?.selected
  const english = i18n.language?.startsWith("en")

  const preview = (id: string) => {
    audio.current?.pause()
    if (playing === id) {
      setPlaying(null)
      return
    }
    const player = new Audio(voiceSampleUrl(id))
    audio.current = player
    player.onended = () => setPlaying((current) => (current === id ? null : current))
    setPlaying(id)
    player.play().catch(() => {
      setPlaying(null)
      toast("error", t("voice.previewFailed"))
    })
  }

  const pick = (voice: AssistantVoice) => {
    if (voice.id === selected || choose.isPending) return
    choose.mutate(voice.id, {
      onSuccess: () => toast("success", t("voice.saved", { name: english ? voice.id : voice.name })),
      onError: () => toast("error", t("voice.saveFailed")),
    })
  }

  if (voices.isError) return <p className="text-n600 text-sm">{t("voice.loadFailed")}</p>

  return (
    <div className="flex flex-col gap-6">
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
                  onPick={() => pick(voice)}
                  onPreview={() => preview(voice.id)}
                />
              ))}
            </div>
          </div>
        )
      })}
      <p className="text-n600 text-xs text-pretty">{t("voice.note")}</p>
    </div>
  )
}
