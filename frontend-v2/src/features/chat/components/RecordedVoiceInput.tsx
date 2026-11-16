import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Keyboard, Mic } from "lucide-react"
import { DialogueRecorder, recordingSupported } from "@/shared/audio/recording"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { makeClientId } from "../lib/message"
import { useConfigQuery } from "../api/config"
import { transcribeRecording, type RecordedVoice, type RecordingDraft } from "../api/recorded-voice"

const drafts = new Map<string, RecordingDraft>()
interface Props {
  draftKey: string
  disabled?: boolean
  answer?: boolean
  onSend: (text: string, voice: RecordedVoice) => void | Promise<void>
}

export function RecordedVoiceInput(props: Props) {
  const user = useAuthStore((s) => s.user?.id)
  const workspace = useWorkspaceStore((s) => s.currentId)
  const config = useConfigQuery()
  if (!user || !workspace || !config.data?.voice_input_enabled) return null
  const key = `recording:${user}:${workspace}:${props.draftKey}`
  return <VoiceControl key={key} {...props} storageKey={key} scope={{ user, workspace }} />
}

function VoiceControl({ storageKey, scope, disabled, answer, onSend }: Props & {
  storageKey: string; scope: { user: string; workspace: string }
}) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const alive = useRef(true)
  const submit = useRef(onSend)
  const blocked = useRef(disabled)
  useLayoutEffect(() => { submit.current = onSend; blocked.current = disabled }, [onSend, disabled])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [phase, setPhase] = useState("")
  const [seconds, setSeconds] = useState(0)
  const [canceling, setCanceling] = useState(false)
  const [error, setError] = useState("")
  const recorder = useRef<DialogueRecorder | null>(null)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const originY = useRef(0)
  const cancel = useRef(false)
  const sending = useRef(false)
  const [pending, setPending] = useState<RecordingDraft | null>(() => {
    const cached = drafts.get(storageKey)
    if (cached) return cached
    try { return JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as RecordingDraft | null } catch { return null }
  })
  const draft = useRef(pending)
  const save = () => {
    if (draft.current) {
      drafts.set(storageKey, draft.current)
      // The blob remains in memory until upload. Its accepted asset and receipt
      // survive refresh; an uncertain send always retains the same client ID.
      if (draft.current.asset_id) {
        try { sessionStorage.setItem(storageKey, JSON.stringify({ ...draft.current, blob: undefined })) } catch { /* retain the in-memory draft */ }
      }
    } else { drafts.delete(storageKey); try { sessionStorage.removeItem(storageKey) } catch { /* storage may be unavailable */ } }
    if (alive.current) setPending(draft.current ? { ...draft.current } : null)
  }
  const stopCapture = () => {
    if (timer.current) clearInterval(timer.current)
    timer.current = null
    recorder.current?.cancel(); recorder.current = null
  }
  useEffect(() => {
    alive.current = true
    const leave = () => { stopCapture(); if (alive.current) { setPhase(""); setSeconds(0) } }
    const hidden = () => { if (document.hidden) leave() }
    window.addEventListener("blur", leave)
    document.addEventListener("visibilitychange", hidden)
    return () => { alive.current = false; stopCapture(); window.removeEventListener("blur", leave); document.removeEventListener("visibilitychange", hidden) }
  }, [])

  const send = async () => {
    if (sending.current || !draft.current || blocked.current) return
    sending.current = true; setBusy(true); setError(""); setPhase("transcribing")
    try {
      const result = await transcribeRecording(draft.current, scope, save)
      if (!alive.current || blocked.current) return
      if (!answer) { result.submitted = true; save() }
      setPhase("sending")
      await submit.current(result.text, { ...result, saveSession: (sessionId) => {
        result.session_id = sessionId; save()
      } })
      draft.current = null; save()
      if (alive.current && answer) setOpen(false)
    } catch (err) {
      save()
      if (alive.current) setError(errorMessage(err))
    } finally {
      sending.current = false
      if (alive.current) { setBusy(false); setPhase("") }
    }
  }
  const finish = (discard = cancel.current) => {
    const current = recorder.current
    if (!current) return
    const blob = discard ? null : current.finish()
    stopCapture(); setPhase("")
    if (!blob) return
    const duration_ms = Math.round((blob.size - 44) / 32)
    if (duration_ms < 600) { setError(t("voiceMessage.tooShort")); return }
    draft.current = { blob, duration_ms, client_id: makeClientId() }
    save(); void send()
  }
  const start = async () => {
    if (recorder.current || sending.current || draft.current || disabled) return
    if (!recordingSupported()) { setError(t("voiceMessage.unsupported")); return }
    cancel.current = false; setCanceling(false); setError(""); setSeconds(0); setPhase("starting")
    const current = recorder.current = new DialogueRecorder()
    try {
      await current.start()
      if (!alive.current || recorder.current !== current) return
      setPhase("recording")
      const began = Date.now()
      timer.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - began) / 1000)
        setSeconds(elapsed)
        if (elapsed >= 60) finish()
      }, 200)
    } catch {
      if (alive.current && recorder.current === current) { stopCapture(); setPhase(""); setError(t("voiceMessage.micFailed")) }
    }
  }
  return <div className={open || pending ? "w-full px-2 py-1" : ""}>
    <div className="flex items-start gap-2">
      <button type="button" disabled={busy || disabled} aria-label={t(open ? "voiceMessage.keyboard" : answer ? "voiceMessage.answer" : "voiceMessage.switchVoice")}
        className="text-n700 hover:bg-hairsoft flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-2 rounded-lg disabled:opacity-40"
        onClick={() => { stopCapture(); setPhase(""); setOpen((v) => !v) }}>
        {open ? <Keyboard size={22} /> : <Mic size={22} />}{answer && <span className="text-sm">{t("voiceMessage.answer")}</span>}
      </button>
      {(open || pending) && <div className="min-w-0 flex-1">
        {pending ? <div className="bg-n100 rounded-lg p-3 text-sm">
          <span>{Math.ceil(pending.duration_ms / 1000)}″</span>
          {busy ? <span className="ms-3" role="status">{t(`voiceMessage.${phase || "transcribing"}`)}</span> : <div className="flex justify-end gap-3">
            {!pending.submitted && <button type="button" onClick={() => { draft.current = null; save(); setError("") }}>{t("voiceMessage.discard")}</button>}
            <button type="button" disabled={disabled} onClick={() => void send()}>{t(answer ? "voiceMessage.retryFill" : "voiceMessage.retry")}</button>
          </div>}
        </div> : <button type="button" disabled={disabled}
          className="bg-n100 text-ink min-h-12 w-full touch-none select-none rounded-lg px-3 py-3 font-medium disabled:opacity-40"
          onContextMenu={(e) => e.preventDefault()}
          onPointerDown={(e) => { if (e.button !== 0) return; e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); originY.current = e.clientY; void start() }}
          onPointerMove={(e) => { if (recorder.current) { cancel.current = e.clientY - originY.current < -60; setCanceling(cancel.current) } }}
          onPointerUp={() => finish()} onPointerCancel={() => finish(true)} onLostPointerCapture={() => { if (recorder.current) finish(true) }}
          onKeyDown={(e) => { if ([" ", "Enter"].includes(e.key)) { e.preventDefault(); if (!e.repeat) void start() } else if (e.key === "Escape") finish(true) }}
          onKeyUp={(e) => { if ([" ", "Enter"].includes(e.key)) { e.preventDefault(); finish() } }}>
          {phase === "recording" ? `${t(canceling ? "voiceMessage.releaseCancel" : answer ? "voiceMessage.releaseFill" : "voiceMessage.releaseSend")} ${seconds}″`
            : t(phase === "starting" ? "voiceMessage.starting" : "voiceMessage.hold")}
        </button>}
        {phase === "recording" && <p className="text-n600 mt-1 text-center text-xs">{t("voiceMessage.slideCancel")}</p>}
      </div>}
    </div>
    {error && <p role="alert" className="text-dangerink mt-1 text-sm">{error}</p>}
  </div>
}
