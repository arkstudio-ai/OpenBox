import { useId, useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import type { Seq } from "../../types/protocol"
import { toSeq } from "../../utils/seq"
import { seqToSlider, sliderMax, sliderToSeq } from "./seekScale"

interface SeekControlProps {
  floor: Seq
  ceiling: Seq
  value: Seq
  onSeek: (seq: Seq) => void
}

/** Jump by dragging across the loaded range or by typing an exact sequence number. */
export function SeekControl({ floor, ceiling, value, onSeek }: SeekControlProps) {
  const { t } = useTranslation("admin-trajectories")
  const inputId = useId()
  const [draft, setDraft] = useState("")
  const max = sliderMax(floor, ceiling)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const seq = toSeq(draft.trim())
    if (seq === null) return
    onSeek(seq)
    setDraft("")
  }
  return (
    <div className="flex min-w-48 flex-1 items-center gap-2">
      <input
        type="range"
        min={0}
        max={max}
        step={1}
        value={seqToSlider(value, floor, ceiling)}
        onChange={(event) => onSeek(sliderToSeq(Number(event.target.value), floor, ceiling))}
        aria-label={t("playback.seek")}
        aria-valuetext={t("playback.seqValue", { seq: value })}
        disabled={max === 0}
        className="accent-a700 min-w-24 flex-1"
        data-testid="trajectory-seek"
      />
      <form onSubmit={submit} className="flex items-center gap-1">
        <label htmlFor={inputId} className="sr-only">
          {t("playback.goTo")}
        </label>
        <input
          id={inputId}
          inputMode="numeric"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t("playback.goToPlaceholder")}
          className="border-hair bg-bg text-ink placeholder:text-n500 w-24 rounded-lg border px-2 py-1 font-mono text-xs"
          data-testid="trajectory-seek-input"
        />
        <button
          type="submit"
          className="border-hair hover:bg-hairsoft rounded-full border px-2.5 py-1 text-xs"
          disabled={toSeq(draft.trim()) === null}
        >
          {t("playback.go")}
        </button>
      </form>
    </div>
  )
}
