import { useState, type ComponentType } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useSearchParams } from "react-router"
import { Settings2 } from "lucide-react"
import { http } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import {
  MEDIA,
  ENTRIES,
  useCategories,
  useMediaScope,
  type Entry,
  type MediaStatus,
  type Review,
  type Template,
} from "./api"
import { button, field, Preview, primary, useMediaText } from "./shared"

const states: MediaStatus[] = ["pending", "listed", "rejected", "superseded", "withdrawn"]
const pageSize = 30
const headers = ["name", "type", "category", "state", "actions"]
const filterLabel = "text-n700 grid min-w-0 gap-1.5 text-xs leading-4 font-medium"
const filterField = cn(field, "h-9 min-w-0 py-0 disabled:bg-hairsoft disabled:text-n600 disabled:cursor-not-allowed")
const inputLabels: Record<string, string> = {
  shop: "shop",
  brief: "brief",
  script: "script",
  product_name: "product",
  poster_title: "headline",
  dishes: "dishes",
  reference_copy: "referenceCopy",
  notes: "notes",
  keep_original: "keepOriginal",
  promotion_brief: "promotion",
  output_language: "language",
  perfect_film: "perfectFilm",
  video_tier: "videoTier",
  music_style: "music",
  beat_music: "beatMusic",
  redo_narration: "narration",
  creative_effects: "effects",
}

const inputKinds: Partial<Record<string, Entry>> = {
  dishes: "menu",
  poster_title: "poster",
  keep_original: "footage_edit",
  redo_narration: "footage_edit",
  creative_effects: "footage_edit",
  reference_copy: "video",
  product_name: "video",
  perfect_film: "video",
  video_tier: "video",
  music_style: "video",
  beat_music: "video",
}

function hasEvidence(value: unknown): boolean {
  if (typeof value === "string") return Boolean(value.trim())
  if (value && typeof value === "object") return Object.keys(value).length > 0
  return value != null
}

function EvidenceValue({ value }: { value: unknown }) {
  const { tx: t } = useMediaText()
  if (typeof value === "boolean") return <>{t(value ? "yes" : "no")}</>
  if (value == null || value === "") return <>—</>
  if (typeof value !== "object") return <>{String(value)}</>
  return <pre className="break-words whitespace-pre-wrap">{JSON.stringify(value, null, 2)}</pre>
}

function TrialInputs({ item }: { item: Review }) {
  const { tx: t } = useMediaText()
  const inputs = Object.entries(item.input ?? {}).filter(
    ([key, value]) => (!inputKinds[key] || inputKinds[key] === item.entry_id) && hasEvidence(value),
  )
  return (
    <section className="border-hair min-w-0 rounded-xl border p-3">
      <h3 className="mb-2 font-medium">{t("trialInputs")}</h3>
      {inputs.length > 0 && (
        <dl className="grid gap-2 text-sm">
          {inputs.map(([key, value]) => (
            <div key={key}>
              <dt className="text-n600 text-xs">{t(inputLabels[key] || "notes")}</dt>
              <dd className="break-words">
                <EvidenceValue value={value} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!inputs.length && !item.materials?.length && <p className="text-n600 text-sm">{t("noEvidence")}</p>}
      {item.materials?.map((media, index) => (
        <div key={index} className="mt-2">
          <Preview media={media} />
        </div>
      ))}
    </section>
  )
}

function Comparison({ row, close }: { row: Template; close: () => void }) {
  const { tx: t } = useMediaText()
  const errorMessage = useApiErrorMessage()
  const scope = useMediaScope()
  const cache = useQueryClient()
  const [choice, setChoice] = useState<"listed" | "rejected" | "withdrawn" | null>(null)
  const [reason, setReason] = useState("")
  const query = useQuery({
    queryKey: [...scope, "review", row.id],
    queryFn: () => http.get<Review>(`${MEDIA}/admin/${row.id}`),
    enabled: row.source !== "builtin",
  })
  const change = useMutation({
    mutationFn: (decision: string) => http.post(`${MEDIA}/admin/${row.id}/decision`, { decision, reason }),
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: [...scope, "admin"] }),
        cache.invalidateQueries({ queryKey: [...scope, "review", row.id] }),
      ])
      setChoice(null)
      setReason("")
    },
  })
  const item = row.source === "builtin" ? ({ ...row, original: row } as Review) : query.data
  return (
    <Dialog open wide onClose={change.isPending ? () => {} : close} label={t("review")}>
      <DialogTitle>
        {row.title}
        {row.version ? ` · v${row.version}` : ""}
      </DialogTitle>
      <DialogBody>{t("reviewDescription")}</DialogBody>
      {query.error && (
        <p role="alert" className="text-dangerink">
          {errorMessage(query.error)}
        </p>
      )}
      {!item && query.isPending && <p role="status">{t("loading")}</p>}
      {item && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <section className="border-hair min-w-0 rounded-xl border p-3">
              <h3 className="mb-2 font-medium">{t("original")}</h3>
              <Preview media={item.original} />
            </section>
            <TrialInputs item={item} />
            <section className="border-hair min-w-0 rounded-xl border p-3">
              <h3 className="mb-2 font-medium">{t("trialResult")}</h3>
              {item.trial_result ? (
                <Preview media={item.trial_result} />
              ) : (
                <p className="text-n600 text-sm">{t("noEvidence")}</p>
              )}
            </section>
          </div>
          {item.recipe && (
            <details className="mt-2 text-sm">
              <summary className="cursor-pointer">{t("recipe")}</summary>
              <p className="mt-2 whitespace-pre-wrap">{item.recipe}</p>
            </details>
          )}
          {!!item.decisions?.length && (
            <section className="mt-2 text-sm">
              <h3 className="font-medium">{t("history")}</h3>
              {item.decisions.map((decision) => (
                <p key={`${decision.created_at}-${decision.decision}`} className="text-n700 mt-1 break-words">
                  {t(`states.${decision.decision}`)} · {new Date(decision.created_at).toLocaleString()} ·{" "}
                  {decision.reviewer_id}
                  {decision.reason ? ` · ${decision.reason}` : ""}
                </p>
              ))}
            </section>
          )}
          {change.error && (
            <p role="alert" className="text-dangerink text-sm">
              {errorMessage(change.error)}
            </p>
          )}
          {choice ? (
            <>
              <label className="grid gap-1 text-sm">
                {t("reviewReason")}
                <textarea
                  className={field}
                  value={reason}
                  maxLength={2000}
                  rows={3}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <DialogActions>
                <button
                  className={button}
                  disabled={change.isPending}
                  onClick={() => {
                    setChoice(null)
                    setReason("")
                  }}
                >
                  {t("cancel")}
                </button>
                <button
                  className={primary}
                  disabled={change.isPending || (choice !== "listed" && !reason.trim())}
                  onClick={() => change.mutate(choice)}
                >
                  {t(`decision.${choice}`)}
                </button>
              </DialogActions>
            </>
          ) : (
            <DialogActions>
              <button className={button} onClick={close}>
                {t("close")}
              </button>
              {item.status === "pending" && (
                <>
                  <button className={button} onClick={() => setChoice("rejected")}>
                    {t("reject")}
                  </button>
                  {item.source === "creator" && (
                    <button className={primary} onClick={() => setChoice("listed")}>
                      {t("approve")}
                    </button>
                  )}
                </>
              )}
              {item.status === "listed" && item.source !== "builtin" && (
                <button className={button} onClick={() => setChoice("withdrawn")}>
                  {t("withdraw")}
                </button>
              )}
            </DialogActions>
          )}
        </>
      )}
    </Dialog>
  )
}

type EditorActions = { onClose: () => void; onSaved: () => void }

interface AdminMediaLibraryProps {
  LegacyExampleEditor: ComponentType<EditorActions & { exampleId: string | null }>
  TaxonomyEditor: ComponentType<EditorActions & { open: boolean }>
}

export function AdminMediaLibrary({ LegacyExampleEditor, TaxonomyEditor }: AdminMediaLibraryProps) {
  const { tx: t } = useMediaText()
  const errorMessage = useApiErrorMessage()
  const scope = useMediaScope()
  const cache = useQueryClient()
  const [params] = useSearchParams()
  const [entry, setEntry] = useState<Entry | "">("")
  const [scene, setScene] = useState("")
  const [status, setStatus] = useState<MediaStatus | "">(() =>
    params.get("status") === "pending" ? "pending" : "",
  )
  const [keyword, setKeyword] = useState("")
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<Template>()
  const [taxonomyOpen, setTaxonomyOpen] = useState(false)
  const categories = useCategories()
  const sceneOptions = categories.data?.entries.find((category) => category.id === entry)?.scenes
  const search = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) })
  if (entry) search.set("entry_id", entry)
  if (scene) search.set("scene", scene)
  if (status) search.set("status", status)
  if (keyword.trim()) search.set("q", keyword.trim())
  const query = useQuery({
    queryKey: [...scope, "admin", search.toString()],
    queryFn: ({ signal }) =>
      http.get<{ items: Template[]; has_more: boolean }>(`${MEDIA}/admin?${search}`, { signal }),
  })
  const refresh = () => {
    void cache.invalidateQueries({ queryKey: [...scope, "admin"] })
  }

  return (
    <section className="@container flex min-w-0 flex-col gap-4">
      <div className="border-hair bg-card grid grid-cols-1 items-end gap-3 rounded-xl border p-4 @min-[28rem]:grid-cols-2 @min-[46rem]:grid-cols-[repeat(3,minmax(0,1fr))_minmax(10rem,1.6fr)_auto]">
        <label className={filterLabel}>
          {t("type")}
          <select
            className={filterField}
            value={entry}
            onChange={(event) => {
              setEntry(event.target.value as Entry | "")
              setScene("")
              setPage(0)
            }}
          >
            <option value="">{t("allTypes")}</option>
            {ENTRIES.map((key) => (
              <option key={key} value={key}>
                {t(`entries.${key}`)}
              </option>
            ))}
          </select>
        </label>
        <label className={filterLabel}>
          {t("category")}
          <select
            className={filterField}
            value={scene}
            disabled={!entry}
            onChange={(event) => {
              setScene(event.target.value)
              setPage(0)
            }}
          >
            <option value="">{t("allCategories")}</option>
            {sceneOptions?.map((child) => (
              <option key={child.id} value={child.id}>
                {child.label}
              </option>
            ))}
          </select>
        </label>
        <label className={filterLabel}>
          {t("state")}
          <select
            className={filterField}
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as MediaStatus | "")
              setPage(0)
            }}
          >
            <option value="">{t("allStates")}</option>
            {states.map((key) => (
              <option key={key} value={key}>
                {t(`states.${key}`)}
              </option>
            ))}
          </select>
        </label>
        <label className={filterLabel}>
          {t("search")}
          <input
            className={filterField}
            type="search"
            value={keyword}
            maxLength={80}
            onChange={(event) => {
              setKeyword(event.target.value)
              setPage(0)
            }}
          />
        </label>
        <button
          type="button"
          className={cn(button, "h-9 justify-self-end whitespace-nowrap @min-[28rem]:col-span-2 @min-[46rem]:col-span-1")}
          onClick={() => setTaxonomyOpen(true)}
        >
          <Settings2 size={16} aria-hidden="true" />
          {t("manageCategories")}
        </button>
      </div>
      {(query.error || categories.error) && (
        <p role="alert" className="text-dangerink">
          {errorMessage(query.error || categories.error)}
        </p>
      )}
      {query.isPending && <p role="status">{t("loading")}</p>}
      {query.data?.items.length === 0 && <p className="text-n600 text-sm">{t("empty")}</p>}
      {!!query.data?.items.length && (
        <div className="border-hair overflow-x-auto rounded-xl border">
          <table className="w-full text-start text-sm">
            <thead className="bg-hairsoft text-n700">
              <tr>
                {headers.map((key) => (
                  <th key={key} scope="col" className="px-3 py-2 text-start font-medium">
                    {t(key)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="bg-card">
              {query.data.items.map((row) => (
                <tr key={row.id} className="border-hair border-t">
                  <td className="px-3 py-2 font-medium">
                    {row.title}
                    {row.version ? ` · v${row.version}` : ""}
                  </td>
                  <td className="px-3 py-2">{t(`entries.${row.entry_id}`)}</td>
                  <td className="px-3 py-2">{row.scene_label || "—"}</td>
                  <td className="px-3 py-2">{t(`states.${row.status}`)}</td>
                  <td className="px-3 py-2">
                    <button type="button" className={button} onClick={() => setSelected(row)}>
                      {t(row.status === "pending" ? "review" : "details")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(page > 0 || query.data?.has_more) && (
        <div className="flex justify-end gap-2">
          <button type="button" className={button} disabled={page === 0} onClick={() => setPage(page - 1)}>
            {t("previous")}
          </button>
          <button
            type="button"
            className={button}
            disabled={!query.data?.has_more}
            onClick={() => setPage(page + 1)}
          >
            {t("next")}
          </button>
        </div>
      )}
      {selected && selected.source !== "example" && (
        <Comparison key={selected.id} row={selected} close={() => setSelected(undefined)} />
      )}
      <LegacyExampleEditor
        exampleId={selected?.source === "example" ? selected.id : null}
        onClose={() => setSelected(undefined)}
        onSaved={refresh}
      />
      {taxonomyOpen && (
        <TaxonomyEditor
          open
          onClose={() => setTaxonomyOpen(false)}
          onSaved={() => {
            void cache.invalidateQueries({ queryKey: [...scope, "categories"] })
            refresh()
          }}
        />
      )}
    </section>
  )
}
