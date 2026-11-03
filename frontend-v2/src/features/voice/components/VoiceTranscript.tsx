// The conversation card of the call window: what was said, as text.
//
// The call page is "listen, do not read": until 2026-11-02 nothing said in
// a call was shown anywhere, so people could not check what they had just
// said or what the assistant was saying (VOICE_SEAM_PLAN §11.2). This lists
// the call's lines from the `transcript` events, the reply in progress at the
// bottom, and names the task the call is about when the server says so.
import { useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { useShallow } from "zustand/react/shallow"
import { useVoiceStore } from "../store"

export function VoiceTranscript({ className }: { className?: string }) {
  const { t } = useTranslation("voice")
  const { lines, focus, bound, hearing, phase } = useVoiceStore(
    useShallow((state) => ({ lines: state.call.transcript, focus: state.call.focus, bound: state.call.bound, hearing: state.call.hearing, phase: state.call.phase })),
  )
  const navigate = useNavigate()
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" })
  }, [lines])
  const live = lines.length > 0 && !lines[lines.length - 1].final ? null : phase === "listening" && hearing ? t("transcript.hearing") : null
  return (
    <section aria-label={t("transcript.title")} data-testid="voice-transcript" className={cn("border-hair mt-3 border-t pt-2", className)}>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-n600 text-xs font-medium">{t("transcript.title")}</span>
        {focus && (
          focus.session_id ? (
            <button type="button" onClick={() => navigate(paths.chat(focus.session_id!))}
              className="text-a700 hover:bg-hairsoft ms-auto max-w-40 truncate rounded-full px-2 text-xs">
              {t("transcript.room", { title: focus.title })}
            </button>
          ) : (
            <span className="text-n600 ms-auto max-w-40 truncate text-xs">{t("transcript.room", { title: focus.title })}</span>
          )
        )}
      </div>
      {focus && (
        <label className="mb-1 flex items-center gap-2 text-xs">
          <input type="checkbox" checked={bound} onChange={(e) => useVoiceStore.getState().setRoom(e.target.checked ? focus.task_id : null)} />
          <span className={bound ? "text-a700" : "text-n700"}>{t(bound ? "transcript.watchOn" : "transcript.watch")}</span>
        </label>
      )}
      {bound && <p className="text-a700 mb-1 text-xs">{t("transcript.inRoom")}</p>}
      <div className="max-h-48 space-y-1 overflow-y-auto pe-1 text-sm" role="log" aria-live="off">
        {lines.length === 0 && <p className="text-n600 text-xs">{t("transcript.empty")}</p>}
        {lines.map((line) => (
          <p key={`${line.role}:${line.itemId}`} className={cn("leading-snug", line.role === "user" ? "text-ink" : "text-n800", !line.final && "italic")}>
            <span className="text-n600 me-1 text-xs">{t(line.role === "user" ? "transcript.you" : "transcript.assistant")}</span>
            {line.text}
          </p>
        ))}
        {live && <p className="text-n600 text-xs italic">{live}</p>}
        {focus?.progress && <p className="text-n600 text-xs italic">{t("transcript.taskDoing", { step: focus.progress })}</p>}
        <div ref={end} />
      </div>
    </section>
  )
}
