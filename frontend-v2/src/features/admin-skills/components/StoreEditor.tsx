import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogTitle } from "@/shared/ui/Dialog"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { useSaveEntry, useStoreDetail, type EntryFields } from "../api/management"
import type { SkillKind, StoreDetail } from "../types"

const FIELD =
  "mt-1 w-full rounded-lg border border-hair bg-bg px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-50"
const BUTTON = "rounded-full border border-hair px-4 py-2 text-sm disabled:opacity-40"
const TEMPLATE = "---\nname: my-skill\ndescription: Describe what this skill does.\n---\n\n# My skill\n"

export function StoreEditor({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { t } = useTranslation("admin-skills")
  const query = useStoreDetail(id)
  const [busy, setBusy] = useState(false)
  const title = t(id ? "manage.edit" : "manage.create")
  return (
    <Dialog
      open
      wide
      label={title}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <DialogTitle>{title}</DialogTitle>
      {id && query.isPending ? (
        <p>{t("list.loading")}</p>
      ) : id && query.isError ? (
        <>
          <p role="alert">{t("list.error")}</p>
          <button onClick={() => void query.refetch()}>{t("manage.retry")}</button>
        </>
      ) : (
        <EditorForm
          key={id ?? "new"}
          entry={id ? query.data : undefined}
          onClose={onClose}
          onBusyChange={setBusy}
        />
      )}
      {id && !query.data && (
        <button className={BUTTON} onClick={onClose}>
          {t("action.cancel")}
        </button>
      )}
    </Dialog>
  )
}

function EditorForm({
  entry: initialEntry,
  onClose,
  onBusyChange,
}: {
  entry?: StoreDetail
  onClose: () => void
  onBusyChange: (busy: boolean) => void
}) {
  const { t } = useTranslation("admin-skills")
  // A draft retains the revision it was based on, even if another mutation
  // invalidates the query while this form is open.
  const [entry] = useState(initialEntry)
  const save = useSaveEntry()
  const [kind, setKind] = useState<SkillKind>(entry?.kind ?? "skill")
  const [name, setName] = useState(entry?.name ?? "")
  const [title, setTitle] = useState(entry?.title ?? "")
  const [description, setDescription] = useState(entry?.description ?? "")
  const [icon, setIcon] = useState(entry?.icon ?? "")
  const [content, setContent] = useState(entry?.content ?? TEMPLATE)
  const [config, setConfig] = useState(
    JSON.stringify(entry?.config ?? { type: "stdio", command: "", args: [] }, null, 2),
  )
  const [error, setError] = useState("")
  const submit = async () => {
    if (save.isPending) return
    setError("")
    let fields: EntryFields = { title: title.trim(), description: description.trim(), icon: icon.trim() }
    if (kind === "skill") {
      if (!entry || content !== entry.content) fields = { ...fields, content }
    } else {
      try {
        const value: unknown = JSON.parse(config)
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error()
        fields.config = value as Record<string, unknown>
      } catch {
        setError(t("manage.invalidJson"))
        return
      }
    }
    onBusyChange(true)
    try {
      await save.mutateAsync({
        id: entry?.catalog_id,
        name: name.trim(),
        kind,
        revision: entry?.revision ?? 0,
        fields,
      })
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : t("dialog.failed"))
    } finally {
      onBusyChange(false)
    }
  }
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <p className="text-n600 text-xs">{t(entry ? "manage.editHint" : "manage.createHint")}</p>
      <fieldset disabled={save.isPending} className="grid min-w-0 gap-3 sm:grid-cols-2">
        <label className="text-sm">
          {t("store.filter.kind")}
          <select
            value={kind}
            disabled={!!entry}
            onChange={(e) => setKind(e.target.value as SkillKind)}
            className={FIELD}
          >
            <option value="skill">{t("store.kind.skill")}</option>
            <option value="mcp">{t("store.kind.mcp")}</option>
          </select>
        </label>
        <label className="text-sm">
          {t("manage.name")}
          <input
            required
            pattern="[a-z0-9]+(-[a-z0-9]+)*"
            maxLength={64}
            disabled={!!entry}
            className={FIELD}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          {t("manage.title")}
          <input
            required
            maxLength={120}
            className={FIELD}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          {t("manage.description")}
          <textarea
            maxLength={4000}
            rows={2}
            className={FIELD}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          {t("manage.icon")}
          <span className="flex items-center gap-2">
            <DisplayIcon icon={icon} className="size-10" />
            <input
              maxLength={2048}
              className={FIELD}
              value={icon}
              onChange={(e) => setIcon(e.target.value)}
            />
          </span>
        </label>
        <label className="text-sm sm:col-span-2">
          {t(kind === "skill" ? "manage.content" : "manage.config")}
          <textarea
            spellCheck={false}
            rows={12}
            maxLength={65536}
            className={`${FIELD} font-mono text-xs`}
            aria-label={t(kind === "skill" ? "manage.content" : "manage.config")}
            value={kind === "skill" ? content : config}
            onChange={(e) => (kind === "skill" ? setContent(e.target.value) : setConfig(e.target.value))}
          />
        </label>
      </fieldset>
      {error && (
        <p role="alert" className="text-danger text-sm break-words">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" className={BUTTON} disabled={save.isPending} onClick={onClose}>
          {t("action.cancel")}
        </button>
        <button
          className={`${BUTTON} bg-ink text-bg`}
          disabled={save.isPending || !title.trim() || !name.trim()}
        >
          {t("manage.save")}
        </button>
      </div>
    </form>
  )
}
