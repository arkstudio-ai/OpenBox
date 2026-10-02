import { useEffect, useRef, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatDateTime } from "@/shared/lib/format"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { DiagnosticData, memoryButton, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { debugApi, type ReplayPreview } from "./api"

const replaySteps = ["route", "retrieval"] as const

export function ReplayDialog({
  runId,
  onClose,
  onCreated,
}: {
  runId: string
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const [steps, setSteps] = useState(["route", "retrieval"])
  const [preview, setPreview] = useState<ReplayPreview | null>(null)
  const [consent, setConsent] = useState(false)
  const [clock, setClock] = useState(() => Date.now())
  const inFlight = useRef(false)
  const prepare = useMutation({
    mutationFn: () => debugApi.preview(runId, steps),
    onSuccess: (result) => {
      setPreview(result)
      setConsent(false)
    },
  })
  const submit = useMutation({
    mutationFn: async () => {
      if (
        !preview?.can_submit ||
        !consent ||
        inFlight.current ||
        Date.parse(preview.expires_at) <= Date.now()
      )
        throw new Error("Replay requires a current preview and explicit cost confirmation")
      inFlight.current = true
      try {
        return await debugApi.replay(preview.preview_id)
      } finally {
        inFlight.current = false
      }
    },
    onSuccess: (result) => onCreated(result.run_id),
  })
  const pending = prepare.isPending || submit.isPending
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const expired = preview ? Date.parse(preview.expires_at) <= clock : false
  const changeStep = (step: string, checked: boolean) => {
    setSteps((current) => (checked ? [...current, step] : current.filter((value) => value !== step)))
    setPreview(null)
    setConsent(false)
    submit.reset()
    prepare.reset()
  }
  return (
    <Dialog open wide onClose={() => !pending && onClose()} label={t("debug.replayTitle")}>
      <DialogTitle>{t("debug.replayTitle")}</DialogTitle>
      <p className="text-n600 text-sm leading-relaxed">{t("debug.replayHint")}</p>
      <fieldset className="my-3 flex flex-wrap gap-4 text-sm" disabled={pending}>
        <legend className="mb-2 font-medium">{t("debug.selectedSteps")}</legend>
        {replaySteps.map((step) => (
          <label key={step} className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={steps.includes(step)}
              onChange={(event) => changeStep(step, event.target.checked)}
            />
            {t(`debug.phase.${step}`)}
          </label>
        ))}
      </fieldset>
      <button
        className={memoryButton}
        disabled={pending || steps.length === 0}
        onClick={() => prepare.mutate()}
      >
        {t(prepare.isPending ? "debug.preparing" : "debug.preview")}
      </button>
      {preview && (
        <div className="mt-3 space-y-3">
          <p className="text-n500 text-xs">
            {t("debug.previewExpires", { time: formatDateTime(preview.expires_at) })}
          </p>
          {(!preview.can_submit || expired) && (
            <p className="text-danger text-sm" role="status">
              {expired ? t("debug.previewExpired") : t("debug.replayUnavailable")}
              {preview.reason_code && <> · {preview.reason_code}</>}
            </p>
          )}
          <details open>
            <summary className="mb-2 text-sm font-medium">{t("debug.previewInput")}</summary>
            <DiagnosticData data={preview.input} />
          </details>
          <details open>
            <summary className="mb-2 text-sm font-medium">{t("debug.previewScopeCalls")}</summary>
            <DiagnosticData data={{ scope: preview.scope, calls: preview.calls }} />
          </details>
          <div>
            <h3 className="mb-2 text-sm font-medium">{t("debug.costEstimate")}</h3>
            <DiagnosticData data={preview.cost_estimate} />
          </div>
          <label className="border-hair flex items-start gap-2 rounded-lg border p-3 text-sm leading-relaxed">
            <input
              type="checkbox"
              checked={consent}
              disabled={pending || !preview.can_submit || expired}
              onChange={(event) => setConsent(event.target.checked)}
              className="mt-1"
            />
            {t("debug.costConsent")}
          </label>
        </div>
      )}
      {(prepare.error || submit.error) && (
        <p role="alert" className="text-danger text-sm">
          {errorText(prepare.error ?? submit.error)}
        </p>
      )}
      <DialogActions>
        <button className={memoryButton} disabled={pending} onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          className={memoryPrimary}
          disabled={pending || !preview?.can_submit || !consent || expired}
          onClick={() => submit.mutate()}
        >
          {t(submit.isPending ? "debug.submitting" : "debug.submitReplay")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
