import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { cn } from "@/shared/lib/cn"
import { useCreateCronJob, useUpdateCronJob } from "@/features/cron/api/cron"
import { useProjectOptions } from "@/features/cron/api/projects"
import {
  INTERVAL_UNITS,
  INTERVAL_UNIT_KEYS,
  MIN_INTERVAL_MINUTES,
  SCHEDULE_MODES,
  SCHEDULE_MODE_KEYS,
  WEEKDAY_KEYS,
} from "@/features/cron/constants"
import {
  DEFAULT_FORM,
  browserTimezone,
  buildSchedule,
  scheduleToForm,
  type ScheduleForm,
} from "@/features/cron/utils/schedule"
import type { CronJob } from "@/features/cron/types"

const inputCls =
  "min-h-9 rounded-lg border border-hair bg-card px-3 text-sm text-ink outline-none focus:border-ink"

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs text-n600">{label}</span>
      {children}
    </label>
  )
}

function FormActions({
  editing,
  valid,
  saving,
  onCancel,
  onSubmit,
}: {
  editing: boolean
  valid: boolean
  saving: boolean
  onCancel: () => void
  onSubmit: () => void
}) {
  const { t } = useTranslation("cron")
  const label = saving ? t("form.saving") : editing ? t("form.save") : t("form.create")
  return (
    <DialogActions>
      <button type="button" onClick={onCancel} className="min-h-9 rounded-full px-4 text-sm text-n700">
        {t("form.cancel")}
      </button>
      <button
        type="button"
        onClick={onSubmit}
        disabled={!valid || saving}
        className={cn(
          "min-h-9 rounded-full bg-ink px-5 text-sm text-bg",
          (!valid || saving) && "opacity-50",
        )}
      >
        {label}
      </button>
    </DialogActions>
  )
}

/** Mode pills + the fields for the active schedule mode. */
function ScheduleFields({
  form,
  patch,
  tz,
}: {
  form: ScheduleForm
  patch: (p: Partial<ScheduleForm>) => void
  tz: string
}) {
  const { t } = useTranslation("cron")
  return (
    <>
      <Field label={t("form.schedule")}>
        <div className="flex flex-wrap gap-1.5">
          {SCHEDULE_MODES.map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={form.mode === mode}
              onClick={() => patch({ mode })}
              className={cn(
                "min-h-8 rounded-full border px-3 text-xs",
                form.mode === mode ? "border-ink bg-n300 text-ink" : "border-hair text-n700",
              )}
            >
              {t(SCHEDULE_MODE_KEYS[mode])}
            </button>
          ))}
        </div>
      </Field>

      {(form.mode === "daily" || form.mode === "weekly") && (
        <div className="flex items-end gap-2.5">
          {form.mode === "weekly" && (
            <Field label={t("form.weekday")}>
              <select
                className={inputCls}
                value={form.weekday}
                onChange={(e) => patch({ weekday: Number(e.target.value) })}
              >
                {Object.entries(WEEKDAY_KEYS).map(([value, key]) => (
                  <option key={value} value={value}>
                    {t(key)}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label={t("form.time")}>
            <input
              type="time"
              className={inputCls}
              value={form.time}
              onChange={(e) => patch({ time: e.target.value })}
            />
          </Field>
          <span className="pb-2 text-[11px] text-n500">{t("form.timezone", { tz })}</span>
        </div>
      )}

      {form.mode === "interval" && (
        <div className="flex items-end gap-2.5">
          <Field label={t("form.every")}>
            <input
              type="number"
              min={1}
              className={cn(inputCls, "w-24")}
              value={form.every}
              onChange={(e) => patch({ every: Number(e.target.value) })}
            />
          </Field>
          <Field label={t("form.unit")}>
            <select
              className={inputCls}
              value={form.unit}
              onChange={(e) => patch({ unit: e.target.value as ScheduleForm["unit"] })}
            >
              {INTERVAL_UNITS.map((unit) => (
                <option key={unit} value={unit}>
                  {t(INTERVAL_UNIT_KEYS[unit])}
                </option>
              ))}
            </select>
          </Field>
          <span className="pb-2 text-[11px] text-n500">
            {t("form.minInterval", { count: MIN_INTERVAL_MINUTES })}
          </span>
        </div>
      )}

      {form.mode === "custom" && (
        <Field label={t("form.expr")}>
          <input
            className={inputCls}
            value={form.expr}
            onChange={(e) => patch({ expr: e.target.value })}
            placeholder="0 9 * * 1-5"
          />
          <span className="text-[11px] text-n500">{t("form.exprHint", { tz })}</span>
        </Field>
      )}
    </>
  )
}

/** Create / edit dialog. Editing keeps the job's session binding. */
export function CronJobForm({
  open,
  onClose,
  job,
}: {
  open: boolean
  onClose: () => void
  job: CronJob | null
}) {
  if (!open) return null
  // Remount per open/target so field state initializes from props — no
  // reset-in-effect needed.
  return <CronJobFormBody key={job?.id ?? "new"} onClose={onClose} job={job} />
}

function CronJobFormBody({ onClose, job }: { onClose: () => void; job: CronJob | null }) {
  const { t } = useTranslation("cron")
  const projects = useProjectOptions()
  const create = useCreateCronJob()
  const update = useUpdateCronJob()

  const [name, setName] = useState(job?.name ?? "")
  const [task, setTask] = useState(job?.task_prompt ?? "")
  const [projectId, setProjectId] = useState(job?.project_id ?? "")
  const [form, setForm] = useState<ScheduleForm>(() =>
    job ? scheduleToForm(job.schedule) : DEFAULT_FORM,
  )
  const [failed, setFailed] = useState(false)

  const tz = browserTimezone()
  const schedule = buildSchedule(form, tz)
  const targetProject = job ? (job.project_id ?? "") : projectId || projects.data?.[0]?.id || ""
  const valid = Boolean(name.trim() && task.trim() && schedule && targetProject)
  const saving = create.isPending || update.isPending

  const patch = (p: Partial<ScheduleForm>) => setForm((f) => ({ ...f, ...p }))

  const submit = () => {
    if (!valid || !schedule || saving) return
    setFailed(false)
    const done = { onSuccess: onClose, onError: () => setFailed(true) }
    if (job) {
      update.mutate(
        { jobId: job.id, patch: { name: name.trim(), task_prompt: task.trim(), schedule } },
        done,
      )
    } else {
      create.mutate(
        { project_id: targetProject, name: name.trim(), schedule, task_prompt: task.trim() },
        done,
      )
    }
  }

  return (
    <Dialog open onClose={onClose}>
      <DialogTitle>{job ? t("form.editTitle") : t("form.createTitle")}</DialogTitle>

      <div className="scr mt-1 flex max-h-[60vh] flex-col gap-3.5 overflow-y-auto pe-1">
        <Field label={t("form.name")}>
          <input
            className={inputCls}
            value={name}
            maxLength={256}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("form.namePlaceholder")}
          />
        </Field>

        <Field label={t("form.task")}>
          <textarea
            className={cn(inputCls, "min-h-24 resize-y py-2 leading-relaxed")}
            value={task}
            maxLength={5000}
            onChange={(e) => setTask(e.target.value)}
            placeholder={t("form.taskPlaceholder")}
          />
        </Field>

        {!job && (
          <Field label={t("form.project")}>
            <select
              className={inputCls}
              value={targetProject}
              onChange={(e) => setProjectId(e.target.value)}
            >
              {(projects.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name?.trim() || p.id}
                </option>
              ))}
            </select>
            <span className="text-[11px] text-n500">{t("form.projectHint")}</span>
          </Field>
        )}

        <ScheduleFields form={form} patch={patch} tz={tz} />

        {failed && <span className="text-pretty text-xs text-danger">{t("form.saveFailed")}</span>}
      </div>

      <FormActions
        editing={Boolean(job)}
        valid={valid}
        saving={saving}
        onCancel={onClose}
        onSubmit={submit}
      />
    </Dialog>
  )
}
