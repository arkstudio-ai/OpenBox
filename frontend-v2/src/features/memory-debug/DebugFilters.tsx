import { useState } from "react"
import { useTranslation } from "react-i18next"
import { memoryButton, memoryCard, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import type { DebugFilters as Filters } from "./api"

const textFields = ["project_id", "session_id", "request_id"] as const
const timeFields = ["since", "until"] as const
const statuses = ["completed", "running", "failed", "degraded", "skipped", "disabled", "expired"] as const
const empty: Filters = { project_id: "", session_id: "", request_id: "", status: "", since: "", until: "" }

export function DebugFilters({
  current,
  loading,
  onApply,
  onRefresh,
}: {
  current: Filters
  loading: boolean
  onApply: (filters: Filters) => void
  onRefresh: () => void
}) {
  const { t } = useTranslation("memory")
  const [draft, setDraft] = useState(current)
  return (
    <form
      className={`${memoryCard} grid gap-3 sm:grid-cols-3`}
      onSubmit={(event) => {
        event.preventDefault()
        onApply(draft)
      }}
      aria-label={t("debug.filters")}
    >
      {textFields.map((key) => (
        <label key={key} className="flex flex-col gap-1.5 text-xs">
          <span>{t(`debug.${key}`)}</span>
          <input
            className={memoryInput}
            value={draft[key]}
            maxLength={128}
            onChange={(event) => setDraft((current) => ({ ...current, [key]: event.target.value }))}
          />
        </label>
      ))}
      <label className="flex flex-col gap-1.5 text-xs">
        <span>{t("debug.status")}</span>
        <select
          className={memoryInput}
          value={draft.status}
          onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value }))}
        >
          <option value="">{t("debug.allStatuses")}</option>
          {statuses.map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
      </label>
      {timeFields.map((key) => (
        <label key={key} className="flex flex-col gap-1.5 text-xs">
          <span>{t(`debug.${key}`)}</span>
          <input
            className={memoryInput}
            type="datetime-local"
            value={draft[key]}
            onChange={(event) => setDraft((current) => ({ ...current, [key]: event.target.value }))}
          />
        </label>
      ))}
      <div className="flex flex-wrap gap-2 sm:col-span-3">
        <button className={memoryPrimary} type="submit">
          {t("debug.applyFilters")}
        </button>
        <button
          className={memoryButton}
          type="button"
          onClick={() => {
            setDraft(empty)
            onApply(empty)
          }}
        >
          {t("debug.clearFilters")}
        </button>
        <button className={memoryButton} type="button" disabled={loading} onClick={onRefresh}>
          {t("refresh")}
        </button>
      </div>
    </form>
  )
}
