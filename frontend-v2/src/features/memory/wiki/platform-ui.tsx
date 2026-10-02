import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import type { ReactNode } from "react"
import { ApiError, type BlobResponse } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { memoryInput } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"

export const panel = "border-hair bg-card min-w-0 space-y-4 rounded-2xl border p-5 sm:p-6"
export function useWikiAction<R, T = void>(fn: (value: T) => Promise<R>, onSuccess?: (result: R) => void) {
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: (result) => {
      void qc.invalidateQueries({ queryKey: key })
      onSuccess?.(result)
    },
  })
}
export function WikiError({ error }: { error: unknown }) {
  const text = useApiErrorMessage()
  const { t } = useTranslation("wiki")
  if (!error) return null
  const code = error instanceof ApiError ? error.code.toLowerCase() : ""
  return (
    <p role="alert" className="text-danger text-sm break-words">
      {t("platformErrors." + code, { defaultValue: text(error) })}
    </p>
  )
}
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-2 text-sm">
      <span>{label}</span>
      {children}
    </label>
  )
}
export function CostConsent({ value, onChange }: { value: boolean; onChange: (value: boolean) => void }) {
  const { t } = useTranslation("wiki")
  return (
    <label className="flex items-start gap-2 text-sm leading-6">
      <input
        type="checkbox"
        className="mt-1.5"
        checked={value}
        onChange={(event) => onChange(event.target.checked)}
      />
      {t("costConsent")}
    </label>
  )
}
export function JsonEditor({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <Field label={label}>
      <textarea
        className={memoryInput + " min-h-48 w-full font-mono text-xs"}
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  )
}
export function saveDownload(result: BlobResponse) {
  const url = URL.createObjectURL(result.blob)
  const link = document.createElement("a")
  link.href = url
  link.download = result.filename ?? "wiki-export"
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export function ScopeHint({ projectId }: { projectId: string }) {
  const { t } = useTranslation("wiki")
  return <p className="text-n500 text-sm">{t(projectId ? "projectSourceScope" : "personalSourceScope")}</p>
}
