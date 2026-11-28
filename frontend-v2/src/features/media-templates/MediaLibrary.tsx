import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router"
import { Clapperboard, Film, Image, Utensils, Sparkles } from "lucide-react"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { http } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import {
  ENTRIES,
  MEDIA,
  useCategories,
  useMediaScope,
  type Entry,
  type Production,
  type Template,
  type Trial,
} from "./api"
import { GuidedProduction } from "./GuidedProduction"
import { button, field, Preview, primary, useMediaText } from "./shared"

const icons = { video: Clapperboard, footage_edit: Film, menu: Utensils, poster: Image }

export function EntryTabs({ entry, onSelect }: { entry: Entry; onSelect: (entry: Entry) => void }) {
  const { tx } = useMediaText()
  return (
    <div className="flex flex-wrap gap-2" aria-label={tx("category")}>
      {ENTRIES.map((key) => {
        const Icon = icons[key]
        return (
          <button
            key={key}
            type="button"
            aria-pressed={entry === key}
            className={cn(button, entry === key && "border-a700 bg-hairsoft text-a700")}
            onClick={() => onSelect(key)}
          >
            <Icon size={16} />
            {tx(`entries.${key}`)}
          </button>
        )
      })}
    </div>
  )
}

function SubmissionDialog({ row, trial, close }: { row: Template; trial: Trial; close: () => void }) {
  const { tx } = useMediaText()
  const scope = useMediaScope()
  const cache = useQueryClient()
  const categories = useCategories()
  const errorMessage = useApiErrorMessage()
  const [scene, setScene] = useState("")
  const [allow, setAllow] = useState(false)
  const submit = useMutation({
    mutationFn: () =>
      http.post(`${MEDIA}/${row.id}/submit`, {
        session_id: trial.session_id,
        scene,
        share_finished_preview: true,
      }),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: [...scope, "mine"] })
      close()
    },
  })
  return (
    <Dialog open label={tx("submit")} onClose={submit.isPending ? () => {} : close}>
      <DialogTitle>{tx("submit")}</DialogTitle>
      <DialogBody>{tx("submitDescription")}</DialogBody>
      <label className="mt-2 grid gap-1 text-sm">
        {tx("category")}
        <select className={field} value={scene} onChange={(event) => setScene(event.target.value)}>
          <option value="">{tx("selectCategory")}</option>
          {categories.data?.entries
            .find((entry) => entry.id === row.entry_id)
            ?.scenes.map((child) => (
              <option key={child.id} value={child.id}>
                {child.label}
              </option>
            ))}
        </select>
      </label>
      <label className="flex items-start gap-2 text-sm">
        <input
          className="mt-1"
          type="checkbox"
          checked={allow}
          onChange={(event) => setAllow(event.target.checked)}
        />
        {tx("consent")}
      </label>
      {(submit.error || categories.error) && (
        <p role="alert" className="text-dangerink text-sm">
          {errorMessage(submit.error || categories.error)}
        </p>
      )}
      <DialogActions>
        <button type="button" className={button} disabled={submit.isPending} onClick={close}>
          {tx("cancel")}
        </button>
        <button
          type="button"
          className={primary}
          disabled={!allow || !scene || submit.isPending}
          onClick={() => submit.mutate()}
        >
          {tx("submit")}
        </button>
      </DialogActions>
    </Dialog>
  )
}

export function MediaLibrary() {
  const { tx, title } = useMediaText()
  const [search, setSearch] = useSearchParams()
  const scope = useMediaScope()
  const cache = useQueryClient()
  const errorMessage = useApiErrorMessage()
  const mine = search.get("tab") === "mine"
  const [entry, setEntry] = useState<Entry>("video")
  const [scene, setScene] = useState("")
  const [preview, setPreview] = useState<Template | null>(null)
  const [active, setActive] = useState<{ sessionId: string; editionId?: string } | null>(null)
  const [submit, setSubmit] = useState<{ row: Template; trial: Trial } | null>(null)
  const categories = useCategories()
  const children = categories.data?.entries.find((category) => category.id === entry)?.scenes ?? []
  const list = useQuery({
    queryKey: [...scope, mine ? "mine" : "catalog", ...(mine ? [] : [entry, scene])],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ entry_id: entry })
      if (scene) params.set("scene", scene)
      return http.get<{ items: Template[] }>(mine ? `${MEDIA}/mine` : `${MEDIA}?${params}`, { signal })
    },
  })
  const start = useMutation({
    mutationFn: async ({ row, trial }: { row: Template; trial: boolean }) => {
      if (trial) return http.post<Production>(`${MEDIA}/${row.id}/trial`)
      const task = await http.post<Production>("/api/light/productions", { entry_id: row.entry_id })
      return http.patch<Production>(`/api/light/productions/${task.session_id}`, {
        revision: task.revision,
        reference_example_id: row.id,
      })
    },
    onSuccess: (task, { row, trial }) => {
      cache.setQueryData([...scope, "production", task.session_id], task)
      if (trial) void cache.invalidateQueries({ queryKey: [...scope, "mine"] })
      setPreview(null)
      setActive({ sessionId: task.session_id, editionId: trial ? row.id : undefined })
    },
  })
  const activeEdition = list.data?.items.find((row) => row.id === active?.editionId) ?? start.variables?.row
  const selectedDraft = search.get("template")
  const switchTab = (owned: boolean) => {
    setSearch(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (owned) next.set("tab", "mine")
        else next.delete("tab")
        next.delete("template")
        return next
      },
      { replace: true },
    )
    start.reset()
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <button
          className={cn(button, !mine && "border-a700 text-a700")}
          aria-pressed={!mine}
          onClick={() => switchTab(false)}
        >
          {tx("library")}
        </button>
        <button
          className={cn(button, mine && "border-a700 text-a700")}
          aria-pressed={mine}
          onClick={() => switchTab(true)}
        >
          {tx("mine")}
        </button>
      </div>
      {!mine && (
        <>
          <EntryTabs
            entry={entry}
            onSelect={(value) => {
              setEntry(value)
              setScene("")
            }}
          />
          <label className="grid max-w-60 gap-1 text-sm">
            {tx("category")}
            <select className={field} value={scene} onChange={(event) => setScene(event.target.value)}>
              <option value="">{tx("allCategories")}</option>
              {children.map((child) => (
                <option key={child.id} value={child.id}>
                  {child.label}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      {mine && (
        <div className="border-hair bg-card flex flex-wrap items-center justify-between gap-3 rounded-xl border p-4">
          <span className="text-n700 text-sm">{tx("authorHint")}</span>
          <Link className={button} to={paths.app}>
            <Sparkles size={16} />
            {tx("openPro")}
          </Link>
        </div>
      )}
      {(list.error || categories.error || start.error) && (
        <p role="alert" className="text-dangerink text-sm">
          {errorMessage(list.error || categories.error || start.error)}
        </p>
      )}
      {list.isPending ? (
        <p role="status">{tx("loading")}</p>
      ) : list.data?.items.length === 0 ? (
        <p className="text-n600 text-sm">{tx(mine ? "emptyMine" : "empty")}</p>
      ) : null}
      {list.data && (
        <div className={cn("grid gap-3", !mine && "sm:grid-cols-2 lg:grid-cols-3")}>
          {list.data.items.map((row) => {
            const trial = row.trials?.find((item) => item.confirmed)
            const lastTrial = row.trials?.[0]
            return (
              <article
                key={row.id}
                className={cn(
                  "border-hair bg-card flex min-w-0 flex-col gap-3 rounded-xl border p-3",
                  row.id === selectedDraft && "border-a700",
                )}
              >
                {!mine && (
                  <button
                    className="block w-full text-start"
                    aria-label={`${tx("preview")} ${title(row)}`}
                    onClick={() => setPreview(row)}
                  >
                    <Preview media={row} thumbnail />
                  </button>
                )}
                <div className="flex items-start justify-between gap-2">
                  <h3 className="text-ink min-w-0 font-medium break-words">
                    {title(row)} {row.version && <span className="text-n600 text-xs">v{row.version}</span>}
                  </h3>
                  {mine && (
                    <span className="bg-hairsoft text-n700 rounded-md px-2 py-1 text-xs whitespace-nowrap">
                      {tx(`states.${row.status}`)}
                    </span>
                  )}
                </div>
                <p className="text-n600 text-xs">
                  {tx(`entries.${row.entry_id}`)}
                  {row.scene_label ? ` · ${row.scene_label}` : ""}
                </p>
                {mine && (
                  <>
                    {row.reason && <p className="text-n700 text-sm">{row.reason}</p>}
                    {row.recipe && (
                      <details className="text-sm">
                        <summary className="cursor-pointer">{tx("recipe")}</summary>
                        <p className="text-n700 mt-2 whitespace-pre-wrap">{row.recipe}</p>
                      </details>
                    )}
                    <div className="mt-auto flex flex-wrap gap-2">
                      {row.source_session_id && (
                        <Link
                          className={button}
                          to={`${paths.chat(row.source_session_id)}?${new URLSearchParams(
                            row.source === "creator"
                              ? { reviseTemplate: row.id }
                              : { templateAsset: row.source_asset_id ?? "" },
                          )}`}
                        >
                          {tx("originalConversation")}
                        </Link>
                      )}
                      {row.status === "draft" &&
                        row.source === "creator" &&
                        row.can_trial !== false &&
                        lastTrial && (
                          <button
                            className={button}
                            onClick={() =>
                              setActive({ sessionId: (trial ?? lastTrial).session_id, editionId: row.id })
                            }
                          >
                            {tx(trial ? "confirmedTrial" : "resumeTrial")}
                          </button>
                        )}
                      {row.status === "draft" && row.source === "creator" && row.can_trial !== false && (
                        <button
                          className={button}
                          disabled={start.isPending}
                          onClick={() => start.mutate({ row, trial: true })}
                        >
                          {tx(lastTrial ? "newTrial" : "startTrial")}
                        </button>
                      )}
                      {row.status === "draft" &&
                        row.source === "creator" &&
                        row.can_trial !== false &&
                        trial && (
                          <button className={primary} onClick={() => setSubmit({ row, trial })}>
                            {tx("submit")}
                          </button>
                        )}
                    </div>
                  </>
                )}
                {!mine && (
                  <button
                    className={cn(button, "mt-auto")}
                    disabled={start.isPending}
                    onClick={() => start.mutate({ row, trial: false })}
                  >
                    {tx("use")}
                  </button>
                )}
              </article>
            )
          })}
        </div>
      )}
      {preview && (
        <Dialog open onClose={() => setPreview(null)} label={tx("preview")} wide>
          <DialogTitle>{title(preview)}</DialogTitle>
          <Preview media={preview} />
          <DialogActions>
            <button
              className={primary}
              disabled={start.isPending}
              onClick={() => start.mutate({ row: preview, trial: false })}
            >
              {tx("use")}
            </button>
          </DialogActions>
        </Dialog>
      )}
      {active && (
        <GuidedProduction
          key={active.sessionId}
          sessionId={active.sessionId}
          edition={active.editionId ? activeEdition : undefined}
          confirmed={activeEdition?.trials?.find((trial) => trial.session_id === active.sessionId)?.confirmed}
          onConfirm={() => setActive(null)}
          onClose={() => setActive(null)}
        />
      )}
      {submit && <SubmissionDialog row={submit.row} trial={submit.trial} close={() => setSubmit(null)} />}
    </section>
  )
}
