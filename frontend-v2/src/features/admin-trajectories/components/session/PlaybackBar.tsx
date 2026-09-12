import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { ChevronLeft, ChevronRight, Pause, Play, Radio, SkipBack, SkipForward } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { PLAYBACK_RATES, type PlaybackRate } from "../../stores/view"
import { SeekControl } from "./SeekControl"
import type { PlaybackActions, PlaybackModel } from "./usePlaybackControls"

interface PlaybackBarProps {
  model: PlaybackModel
  actions: PlaybackActions
}

interface IconButtonProps {
  label: string
  onClick: () => void
  disabled?: boolean
  pressed?: boolean
  testId: string
  children: ReactNode
}

function IconButton({ label, onClick, disabled, pressed, testId, children }: IconButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      data-testid={testId}
      className="text-n700 hover:bg-hairsoft hover:text-ink inline-flex size-8 items-center justify-center rounded-full disabled:opacity-30"
    >
      {children}
    </button>
  )
}

/**
 * Replay of what was recorded — play, pause, step by event or Step, seek,
 * speed and idle skipping. It moves the viewing position only; it cannot
 * send, approve, answer, resume or cancel anything in the watched session.
 */
export function PlaybackBar({ model, actions }: PlaybackBarProps) {
  const { t } = useTranslation("admin-trajectories")
  const go = (seq: string | null) => () => seq && actions.seek(seq)
  return (
    <section
      aria-label={t("playback.title")}
      className="border-hair bg-card flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border px-3 py-2"
      data-testid="trajectory-playback"
    >
      <div role="group" aria-label={t("playback.controls")} className="flex items-center">
        <IconButton
          label={t("playback.previousStep")}
          onClick={go(model.previousStep)}
          disabled={!model.previousStep}
          testId="trajectory-previous-step"
        >
          <SkipBack size={15} aria-hidden />
        </IconButton>
        <IconButton
          label={t("playback.previousEvent")}
          onClick={go(model.previousEvent)}
          disabled={!model.previousEvent}
          testId="trajectory-previous-event"
        >
          <ChevronLeft size={16} aria-hidden />
        </IconButton>
        <IconButton
          label={t(model.playing ? "playback.pause" : "playback.play")}
          onClick={actions.togglePlay}
          pressed={model.playing}
          testId="trajectory-play"
        >
          {model.playing ? <Pause size={16} aria-hidden /> : <Play size={16} aria-hidden />}
        </IconButton>
        <IconButton
          label={t("playback.nextEvent")}
          onClick={go(model.nextEvent)}
          disabled={!model.nextEvent}
          testId="trajectory-next-event"
        >
          <ChevronRight size={16} aria-hidden />
        </IconButton>
        <IconButton
          label={t("playback.nextStep")}
          onClick={go(model.nextStep)}
          disabled={!model.nextStep}
          testId="trajectory-next-step"
        >
          <SkipForward size={15} aria-hidden />
        </IconButton>
      </div>
      <SeekControl
        floor={model.floor}
        ceiling={model.loadedSeq}
        value={model.current}
        onSeek={actions.seek}
      />
      <label className="text-n600 flex items-center gap-1.5 text-xs">
        {t("playback.rate")}
        <select
          value={model.rate}
          onChange={(event) => actions.setRate(Number(event.target.value) as PlaybackRate)}
          className="border-hair bg-bg text-ink rounded-lg border px-1.5 py-1 text-xs"
          data-testid="trajectory-rate"
        >
          {PLAYBACK_RATES.map((rate) => (
            <option key={rate} value={rate}>
              {t("playback.rateValue", { rate })}
            </option>
          ))}
        </select>
      </label>
      <label className="text-n600 flex items-center gap-1.5 text-xs">
        <input
          type="checkbox"
          checked={model.skipIdle}
          onChange={(event) => actions.setSkipIdle(event.target.checked)}
          data-testid="trajectory-skip-idle"
        />
        {t("playback.skipIdle")}
      </label>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          className={cn(
            "text-2xs inline-flex items-center gap-1 rounded-full px-2 py-0.5",
            model.live ? "bg-s100 text-s800" : "bg-a100 text-a700",
          )}
          data-testid="trajectory-mode"
          data-live={model.live ? "true" : "false"}
        >
          {model.live && <Radio size={11} aria-hidden />}
          {t(model.live ? "playback.live" : "playback.replay")}
        </span>
        {model.loading && <Spinner className="size-3.5" />}
        <span className="text-ink font-mono" data-testid="trajectory-effective-seq" data-seq={model.current}>
          {t("playback.position", { seq: model.current })}
        </span>
        <span className="text-n500 font-mono" data-testid="trajectory-live-head" data-seq={model.headSeq}>
          {t("playback.head", { seq: model.headSeq })}
        </span>
        {!model.live && model.newer !== "0" && (
          <span role="status" className="text-a700" data-testid="trajectory-newer-count">
            {t("playback.newer", { count: model.newer === "1" ? 1 : 2, n: model.newer })}
          </span>
        )}
        {!model.live && (
          <button
            type="button"
            onClick={actions.returnToLive}
            className="border-hair hover:bg-hairsoft rounded-full border px-2.5 py-1"
            data-testid="trajectory-return-live"
          >
            {t("playback.returnToLive")}
          </button>
        )}
      </div>
    </section>
  )
}
