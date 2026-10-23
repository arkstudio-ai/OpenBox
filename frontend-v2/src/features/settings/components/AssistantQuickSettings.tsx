import { useEffect, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { Play, Square } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ASSISTANT_TONES, CALL_DETAILS, type AssistantProfile } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { toast } from "@/shared/ui/Toast"
import { useAssistantVoices, useChooseVoice, voiceSampleUrl } from "../api/voice"
import { Choices, Field, Switch } from "./controls"
import { CompanionAppearanceSettings } from "./CompanionAppearanceSettings"

interface Props {
  inCall: boolean
  onResetPosition?: () => void
  onNavigate: () => void
}
const selectClass = "bg-card border-hair text-ink min-h-11 w-full min-w-0 rounded-xl border px-3 text-sm"

function QuickVoiceSettings({ inCall }: Pick<Props, "inCall">) {
  const { t, i18n } = useTranslation("companion")
  const voices = useAssistantVoices()
  const choose = useChooseVoice()
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  useEffect(
    () => () => {
      audio.current?.pause()
    },
    [voices.data?.model, voices.data?.selected],
  )
  useEffect(() => {
    if (inCall) audio.current?.pause()
  }, [inCall])
  const stop = () => {
    audio.current?.pause()
    audio.current = null
    setPlaying(false)
  }
  const save = (choice: { model?: string; voice?: string }) => {
    stop()
    choose.mutate(choice, {
      onSuccess: () => toast("success", t("settings.voiceSaved")),
      onError: () => toast("error", t("settings.saveFailed")),
    })
  }
  const preview = () => {
    if (playing) {
      stop()
      return
    }
    if (!voices.data) return
    const player = new Audio(voiceSampleUrl(voices.data.selected, voices.data.model))
    audio.current = player
    player.onended = () => {
      if (audio.current === player) setPlaying(false)
    }
    player.onpause = () => {
      if (audio.current === player) setPlaying(false)
    }
    setPlaying(true)
    void player.play().catch(() => {
      if (audio.current !== player) return
      setPlaying(false)
      toast("error", t("settings.previewFailed"))
    })
  }
  const automatic = !!voices.data?.voice_source && voices.data.voice_source !== "user"
  const selectedVoice = voices.data?.voices.find((voice) => voice.id === voices.data?.selected)
  return (
    <section className="space-y-4" aria-label={t("settings.voiceTitle")}>
      <h3 className="text-sm font-medium">{t("settings.voiceTitle")}</h3>
      {voices.isError ? (
        <p role="alert" className="text-danger-ink text-xs">
          {t("settings.loadFailed")}
          <button type="button" onClick={() => void voices.refetch()} className="ms-2 underline">
            {t("retry")}
          </button>
        </p>
      ) : (
        <>
          <Field label={t("settings.model")}>
            {(id) => (
              <select
                id={id}
                className={selectClass}
                value={voices.data?.model ?? ""}
                disabled={!voices.data || choose.isPending}
                onChange={(event) => save({ model: event.target.value })}
              >
                {!voices.data && <option value="">{t("loading")}</option>}
                {voices.data?.models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label={t("settings.voice")}>
            {(id) => (
              <div className="flex gap-2">
                <select
                  id={id}
                  className={selectClass}
                  value={automatic ? "" : (voices.data?.selected ?? "")}
                  disabled={!voices.data || choose.isPending}
                  onChange={(event) => save({ voice: event.target.value, model: voices.data?.model })}
                >
                  {!voices.data && <option value="">{t("loading")}</option>}
                  {automatic && (
                    <option value="" disabled>
                      {t("settings.autoVoice", {
                        name: i18n.language.startsWith("en") ? selectedVoice?.id : selectedVoice?.name,
                      })}
                    </option>
                  )}
                  {voices.data?.voices.map((voice) => (
                    <option key={voice.id} value={voice.id}>
                      {i18n.language.startsWith("en") ? voice.id : voice.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={preview}
                  disabled={!voices.data || choose.isPending || inCall}
                  title={t(inCall ? "settings.previewInCall" : "settings.preview")}
                  aria-label={t(playing ? "settings.stopPreview" : "settings.preview")}
                  className="border-hair hover:bg-hairsoft flex size-11 shrink-0 items-center justify-center rounded-xl border disabled:opacity-40"
                >
                  {playing ? <Square size={15} aria-hidden /> : <Play size={16} aria-hidden />}
                </button>
              </div>
            )}
          </Field>
          <p className="text-n700 text-xs leading-5">
            {t(inCall ? "settings.nextCall" : "settings.voiceHint")}
          </p>
        </>
      )}
    </section>
  )
}

function QuickConversationSettings() {
  const { t } = useTranslation(["companion", "settings"])
  const userId = useAuthStore((state) => state.user?.id)
  const profile = useAppearanceStore((state) => state.assistant)
  const save = useMutation({
    scope: { id: `assistant-profile:${userId}` },
    mutationFn: (patch: Partial<AssistantProfile>) => {
      if (useAuthStore.getState().user?.id !== userId) throw new Error("accountChanged")
      return useAppearanceStore.getState().setAssistantProfile(patch)
    },
    onSuccess: () => toast("success", t("settings.saved")),
    onError: () => toast("error", t("settings.saveFailed")),
  })
  return (
    <section className="space-y-4" aria-label={t("settings.conversation")}>
      <Choices
        label={t("settings.tone")}
        options={ASSISTANT_TONES}
        value={profile.tone}
        text={(tone) => t(`assistant.tone.${tone}`, { ns: "settings" })}
        disabled={save.isPending}
        onPick={(tone) => save.mutate({ tone })}
      />
      <Choices
        label={t("settings.detail")}
        options={CALL_DETAILS}
        value={profile.call_detail}
        text={(detail) => t(`voice.call.detailOption.${detail}`, { ns: "settings" })}
        disabled={save.isPending}
        onPick={(call_detail) => save.mutate({ call_detail })}
      />
      <Switch
        on={profile.call_recap}
        label={t("settings.recap")}
        disabled={save.isPending}
        onToggle={() => save.mutate({ call_recap: !profile.call_recap })}
      />
      <Switch
        on={profile.call_reports}
        label={t("settings.reports")}
        disabled={save.isPending}
        onToggle={() => save.mutate({ call_reports: !profile.call_reports })}
      />
    </section>
  )
}

export function AssistantQuickSettings({ inCall, onResetPosition, onNavigate }: Props) {
  const { t } = useTranslation("companion")
  return (
    <div className="scr h-full overflow-y-auto overscroll-contain px-5 py-4">
      <div className="divide-hair space-y-5 divide-y [&>section]:pb-5">
        <CompanionAppearanceSettings compact />
        <QuickVoiceSettings inCall={inCall} />
        <QuickConversationSettings />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 pt-4 text-xs">
        {onResetPosition && (
          <button
            type="button"
            onClick={onResetPosition}
            className="text-n700 hover:text-ink min-h-11 underline underline-offset-4"
          >
            {t("settings.resetPosition")}
          </button>
        )}
        <Link
          to={paths.settings("assistant")}
          onClick={onNavigate}
          className="text-ink min-h-11 content-center underline underline-offset-4"
        >
          {t("settings.all")}
        </Link>
      </div>
    </div>
  )
}
