import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import {
  RECORDING_STATUS_FILTERS,
  RECORDING_STATUS_LABELS,
  RUN_STATUS_FILTERS,
  RUN_STATUS_LABELS,
} from "../constants/labels"
import { EMPTY_LIST_PARAMS, type ListParams } from "../utils/params"

interface Props {
  params: ListParams
  onApply: (patch: Partial<ListParams>) => void
  onReset: () => void
}

type Draft = Pick<
  ListParams,
  "userQuery" | "userId" | "q" | "workspaceId" | "status" | "recording" | "from" | "to" | "includeUnrecorded"
>

const FIELD =
  "border-hair bg-bg text-ink placeholder:text-n500 min-w-0 rounded-lg border px-2.5 py-1.5 text-sm"
const LABEL = "text-n600 flex min-w-0 flex-col gap-1 text-xs"
const BUTTON = "border-hair hover:bg-hairsoft rounded-full border px-3.5 py-1.5 text-sm disabled:opacity-40"

function draftOf(params: ListParams): Draft {
  const { userQuery, userId, q, workspaceId, status, recording, from, to, includeUnrecorded } = params
  return { userQuery, userId, q, workspaceId, status, recording, from, to, includeUnrecorded }
}

/** ISO instant ⇄ the `datetime-local` control, which has no zone and no seconds field. */
function toLocalInput(iso: string): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

function fromLocalInput(value: string): string {
  if (!value) return ""
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "" : date.toISOString()
}

/** Server-side filters. Typing edits a draft; nothing is requested until Apply. */
export function SessionFilters({ params, onApply, onReset }: Props) {
  const { t } = useTranslation("admin-trajectories")
  const [draft, setDraft] = useState<Draft>(() => draftOf(params))
  const [source, setSource] = useState(params)
  // The URL can change underneath (back button, reset); follow it during render.
  if (source !== params) {
    setSource(params)
    setDraft(draftOf(params))
  }
  const set = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }))
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onApply({
      ...draft,
      userQuery: draft.userQuery.trim(),
      userId: draft.userId.trim(),
      q: draft.q.trim(),
      workspaceId: draft.workspaceId.trim(),
    })
  }
  const dirty = JSON.stringify(draftOf(EMPTY_LIST_PARAMS)) !== JSON.stringify(draftOf(params))

  return (
    <form
      onSubmit={submit}
      className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4"
      role="search"
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className={LABEL}>
          {t("list.filter.owner")}
          <input
            className={FIELD}
            value={draft.userQuery}
            onChange={(e) => set({ userQuery: e.target.value })}
            placeholder={t("list.filter.ownerPlaceholder")}
          />
        </label>
        <label className={LABEL}>
          {t("list.filter.userId")}
          <input
            className={`${FIELD} font-mono`}
            value={draft.userId}
            onChange={(e) => set({ userId: e.target.value })}
            placeholder={t("list.filter.userIdPlaceholder")}
          />
        </label>
        <label className={LABEL}>
          {t("list.filter.session")}
          <input
            className={FIELD}
            value={draft.q}
            onChange={(e) => set({ q: e.target.value })}
            placeholder={t("list.filter.sessionPlaceholder")}
          />
        </label>
        <label className={LABEL}>
          {t("list.filter.workspace")}
          <input
            className={`${FIELD} font-mono`}
            value={draft.workspaceId}
            onChange={(e) => set({ workspaceId: e.target.value })}
            placeholder={t("list.filter.workspacePlaceholder")}
          />
        </label>
        <label className={LABEL}>
          {t("list.filter.status")}
          <select className={FIELD} value={draft.status} onChange={(e) => set({ status: e.target.value })}>
            <option value="">{t("list.filter.any")}</option>
            {RUN_STATUS_FILTERS.map((value) => (
              <option key={value} value={value}>
                {t(RUN_STATUS_LABELS[value])}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          {t("list.filter.recording")}
          <select
            className={FIELD}
            value={draft.recording}
            onChange={(e) => set({ recording: e.target.value })}
          >
            <option value="">{t("list.filter.any")}</option>
            {RECORDING_STATUS_FILTERS.map((value) => (
              <option key={value} value={value}>
                {t(RECORDING_STATUS_LABELS[value])}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          {t("list.filter.from")}
          <input
            type="datetime-local"
            className={FIELD}
            value={toLocalInput(draft.from)}
            onChange={(e) => set({ from: fromLocalInput(e.target.value) })}
          />
        </label>
        <label className={LABEL}>
          {t("list.filter.to")}
          <input
            type="datetime-local"
            className={FIELD}
            value={toLocalInput(draft.to)}
            onChange={(e) => set({ to: fromLocalInput(e.target.value) })}
          />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-n800 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={draft.includeUnrecorded}
            onChange={(e) => set({ includeUnrecorded: e.target.checked })}
          />
          {t("list.filter.includeUnrecorded")}
        </label>
        <span className="flex-1" />
        <button type="button" className={BUTTON} onClick={onReset} disabled={!dirty}>
          {t("list.filter.reset")}
        </button>
        <button type="submit" className="bg-ink text-bg rounded-full px-4 py-1.5 text-sm">
          {t("list.filter.apply")}
        </button>
      </div>
    </form>
  )
}
