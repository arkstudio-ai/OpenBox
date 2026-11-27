import { useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { paths } from "@/shared/router/paths"
import { Dialog, DialogTitle } from "@/shared/ui/Dialog"
import { uploadResource } from "../api/upload"

const api = "/api/video-templates"
const studioTabs = ["catalog", "author", "mine"] as const
const field = "border-hair bg-bg text-ink w-full rounded-lg border px-3 py-2"
const button = "border-hair text-ink rounded-full border px-4 py-2 disabled:opacity-45"
const primary = "bg-ink text-bg rounded-full px-4 py-2 disabled:opacity-45"
interface Pin {
  template_id: string
  edition_id: string
  version: number
  sha256: string
}
interface Draft {
  source_asset_id: string
  template_ref: Pin
  title: string
  direction: string
  status: string
  reference_pack: string
  cast?: { label: string; file: string }[]
  rhythm: { seconds: number; cuts: unknown[] }
}
interface Source {
  asset_id: string
  name: string
}
interface Card {
  example_id: string
  title: string
  media_url: string
  poster_url: string
  template_ref: Pin
}
interface Form {
  session_id: string
  revision: number
}

export function VideoTemplateStudio({ sourceId }: { sourceId?: string }) {
  const [open, setOpen] = useState(false)
  const workspace = useWorkspaceStore((s) => s.currentId)
  const { t } = useTranslation("resources")
  const title = t(sourceId ? "templates.make" : "templates.center")
  return (
    <>
      <button type="button" className={button} onClick={() => setOpen(true)}>
        {title}
      </button>
      <Dialog open={open} wide label={title} onClose={() => setOpen(false)}>
        {open && <Studio key={workspace} sourceId={sourceId} close={() => setOpen(false)} />}
      </Dialog>
    </>
  )
}

function Studio({ sourceId, close }: { sourceId?: string; close: () => void }) {
  const { t } = useTranslation("resources", { keyPrefix: "templates" })
  const [tab, setTab] = useState(sourceId ? "author" : "catalog")
  const [source, setSource] = useState(sourceId ?? "")
  const [parent, setParent] = useState<Draft | null>(null)
  const [using, setUsing] = useState<Card | null>(null)
  const workspace = useWorkspaceStore((s) => s.currentId)
  const sources = useQuery({
    queryKey: ["video-template-sources", workspace],
    queryFn: () => http.get<{ items: Source[] }>(api + "/sources"),
    enabled: tab === "author",
  })
  const list = useQuery({
    queryKey: ["video-template-catalog", workspace, tab],
    queryFn: () => http.get<{ items: Card[] }>(api),
    enabled: tab === "catalog",
  })
  const mine = useQuery({
    queryKey: ["video-template-mine", workspace],
    queryFn: () => http.get<{ items: Draft[] }>(api + "/mine"),
    enabled: tab === "mine",
  })
  return (
    <>
      <div className="flex items-center justify-between">
        <DialogTitle>{t("center")}</DialogTitle>
        <button onClick={close}>{t("close")}</button>
      </div>
      <nav className="flex flex-wrap gap-2">
        {studioTabs.map((k) => (
          <button
            key={k}
            className={tab === k ? primary : button}
            onClick={() => {
              setTab(k)
              setParent(null)
              setUsing(null)
            }}
          >
            {t(k)}
          </button>
        ))}
      </nav>
      {tab === "catalog" &&
        (using ? (
          <UseTemplate template={using} close={close} />
        ) : (
          <>
            <button className="self-start text-sm underline" onClick={() => void list.refetch()}>
              {t("refresh")}
            </button>
            {list.isSuccess && !list.data.items.length && <p>{t("empty")}</p>}
            {list.data?.items.map((item) => (
              <article key={item.example_id} className="border-hair flex items-center gap-4 border-b py-3">
                <video
                  className="h-28 w-20 rounded-lg object-cover"
                  poster={item.poster_url}
                  controls
                  preload="none"
                  src={item.media_url}
                />
                <div className="flex flex-1 flex-col gap-2">
                  <strong>{item.title}</strong>
                  <span>{t("version", { version: item.template_ref.version })}</span>
                </div>
                <button className={button} onClick={() => setUsing(item)}>
                  {t("use")}
                </button>
              </article>
            ))}
          </>
        ))}
      {tab === "author" && (
        <AuthorSource
          items={sources.data?.items}
          sourceId={sourceId}
          parent={parent}
          source={source}
          onSelect={setSource}
        />
      )}
      {tab === "mine" &&
        mine.data?.items.map((d) => (
          <Saved
            key={d.template_ref.edition_id}
            initial={d}
            revise={() => {
              setParent(d)
              setSource(d.source_asset_id)
              setTab("author")
            }}
          />
        ))}
      {tab === "mine" && mine.isSuccess && !mine.data.items.length && <p>{t("noDrafts")}</p>}
      {(tab === "mine" ? mine.error : tab === "catalog" ? list.error : sources.error) && (
        <p role="alert">{t("loadError")}</p>
      )}
    </>
  )
}

function AuthorSource({
  items,
  sourceId,
  parent,
  source,
  onSelect,
}: {
  items?: Source[]
  sourceId?: string
  parent: Draft | null
  source: string
  onSelect: (value: string) => void
}) {
  const { t } = useTranslation("resources", { keyPrefix: "templates" })
  const known = parent?.source_asset_id ?? sourceId
  const options = [...(items ?? [])]
  if (known && !options.some((item) => item.asset_id === known)) {
    options.unshift({ asset_id: known, name: parent?.title ?? t("source") })
  }
  return (
    <>
      <label>
        {t("source")}
        <select className={field} value={source} onChange={(e) => onSelect(e.target.value)}>
          <option value="">{t("choose")}</option>
          {options.map((item) => (
            <option key={item.asset_id} value={item.asset_id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      {items &&
        source &&
        (options.some((item) => item.asset_id === source) ? (
          <Editor key={source + (parent?.template_ref.edition_id ?? "")} source={source} parent={parent} />
        ) : (
          <p role="alert">{t("unavailable")}</p>
        ))}
    </>
  )
}

function Editor({ source, parent }: { source: string; parent: Draft | null }) {
  const { t } = useTranslation("resources", { keyPrefix: "templates" })
  const [title, setTitle] = useState(parent?.title ?? "")
  const [direction, setDirection] = useState(parent?.direction ?? "")
  const [pack, setPack] = useState(parent?.reference_pack ?? "")
  const [portraits, setPortraits] = useState<{ file: File; label: string }[]>([])
  const [inheritCast, setInheritCast] = useState(Boolean(parent?.cast?.length))
  const uploaded = useRef(new WeakMap<File, string>())
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  async function submit(analyze: boolean) {
    setBusy(true)
    setError("")
    try {
      if (analyze) {
        const result = await http.post<{ direction: string }>(
          api + "/sources/" + encodeURIComponent(source) + "/analyze",
        )
        setDirection(result.direction)
      } else {
        const cast = []
        for (const { file, label } of inheritCast ? [] : portraits) {
          let id = uploaded.current.get(file)
          if (!id) {
            id = (await uploadResource(file, null, () => {})).id
            uploaded.current.set(file, id)
          }
          cast.push({ asset_id: id, label })
        }
        setDraft(
          await http.post<Draft>(api + "/drafts", {
            asset_id: source,
            title,
            direction,
            reference_pack: pack,
            revises_edition_id: parent?.template_ref.edition_id,
            cast_images: inheritCast ? undefined : cast,
          }),
        )
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  if (draft)
    return (
      <>
        <Saved initial={draft} />
        <button className={button} onClick={() => setDraft(null)}>
          {t("edit")}
        </button>
      </>
    )
  return (
    <>
      <label>
        {t("title")}
        <input className={field} maxLength={88} value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      {Boolean(parent?.cast?.length) && (
        <label>
          <input type="checkbox" checked={inheritCast} onChange={(e) => setInheritCast(e.target.checked)} />
          {t("inheritCast")} · {parent?.cast?.map((p) => p.label).join(" / ")}
        </label>
      )}
      {!inheritCast && (
        <label>
          {t("castImages")}
          <input
            className={field}
            type="file"
            multiple
            accept="image/jpeg,image/png,image/webp"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? [])
              if (files.length > 6 || files.some((f) => f.size > 10 * 1024 * 1024)) {
                setError(t("castLimit"))
                e.target.value = ""
                setPortraits([])
                return
              }
              setError("")
              setPortraits(
                files.map((file) => ({ file, label: file.name.replace(/\.[^.]+$/, "").slice(0, 150) })),
              )
            }}
          />
        </label>
      )}
      {!inheritCast &&
        portraits.map((p, i) => (
          <label key={i}>
            {t("castLabel")} · {p.file.name}
            <input
              value={p.label}
              maxLength={150}
              className={field}
              onChange={(e) =>
                setPortraits(
                  portraits.map((item, j) => (j === i ? { ...item, label: e.target.value } : item)),
                )
              }
            />
          </label>
        ))}
      <label>
        {t("direction")}
        <textarea
          className={field}
          rows={6}
          maxLength={5500}
          value={direction}
          onChange={(e) => setDirection(e.target.value)}
        />
      </label>
      <button className={button} disabled={busy} onClick={() => void submit(true)}>
        {t("analyze")}
      </button>
      <label>
        {t("pack")}
        <select className={field} value={pack} onChange={(e) => setPack(e.target.value)}>
          <option value="">{t("none")}</option>
          <option value="gallery-stage">{t("stage")}</option>
        </select>
      </label>
      {error && <p role="alert">{error}</p>}
      <button
        className={primary}
        disabled={
          busy ||
          title.trim().length < 2 ||
          direction.trim().length < 20 ||
          (!inheritCast && portraits.some((p) => !p.label.trim()))
        }
        onClick={() => void submit(false)}
      >
        {busy ? t("working") : t("save")}
      </button>
    </>
  )
}

function Saved({ initial, revise }: { initial: Draft; revise?: () => void }) {
  const { t } = useTranslation("resources", { keyPrefix: "templates" })
  const [d, setDraft] = useState(initial)
  const [preview, setPreview] = useState(false)
  const workspace = useWorkspaceStore((s) => s.currentId)
  const source = useQuery({
    queryKey: ["video-template-private-preview", workspace, d.source_asset_id],
    queryFn: () =>
      http.get<{ url: string; name: string }>(api + "/sources/" + encodeURIComponent(d.source_asset_id)),
    enabled: preview && Boolean(d.source_asset_id),
  })
  const [share, setShare] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  async function publish() {
    setBusy(true)
    setError("")
    try {
      setDraft(
        await http.post<Draft>(api + "/" + d.template_ref.edition_id + "/publish", {
          sha256: d.template_ref.sha256,
          share_finished_preview: true,
          share_cast_images: Boolean(d.cast?.length),
        }),
      )
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <article className="border-hair flex flex-col gap-3 rounded-xl border p-4">
      <strong>{t("edition", { title: d.title, version: d.template_ref.version })}</strong>
      <p>{t("timing", { seconds: d.rhythm.seconds, count: d.rhythm.cuts.length })}</p>
      <details>
        <summary>{t("direction")}</summary>
        <p className="whitespace-pre-wrap">{d.direction}</p>
        {Boolean(d.cast?.length) && (
          <ul>
            {d.cast?.map((p) => (
              <li key={p.file}>{p.label}</li>
            ))}
          </ul>
        )}
        <p>
          {t("pack")} · {t(d.reference_pack ? "stage" : "none")}
        </p>
      </details>
      <button className={button} onClick={() => setPreview(!preview)}>
        {t("preview")}
      </button>
      {preview && source.data && (
        <video
          className="max-h-60 self-center"
          controls
          src={source.data.url}
          aria-label={source.data.name}
        />
      )}
      {preview && source.isError && <p role="alert">{t("unavailable")}</p>}
      <p role="status">{t("state." + d.status, { defaultValue: d.status })}</p>
      {revise && (
        <button className={button} onClick={revise}>
          {t("revise")}
        </button>
      )}
      {["unpublished", "withdrawn"].includes(d.status) && (
        <>
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} />
            {t(d.cast?.length ? "castConsent" : "consent")}
          </label>
          <button className={primary} disabled={!share || busy} onClick={() => void publish()}>
            {t("publish")}
          </button>
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </article>
  )
}

function UseTemplate({ template, close }: { template: Card; close: () => void }) {
  const { t } = useTranslation("resources", { keyPrefix: "templates" })
  const navigate = useNavigate()
  const [product, setProduct] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const [shop, setShop] = useState("")
  const [offer, setOffer] = useState("")
  const [script, setScript] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const upload = useRef<{ file: File; id: string } | null>(null)
  const draft = useRef<Form | null>(null)
  const [run, setRun] = useState<{ form: Form; requestId: string } | null>(null)
  async function start() {
    if (!file) return
    setBusy(true)
    setError("")
    try {
      let saved = run
      if (!saved) {
        if (upload.current?.file !== file) {
          const asset = await uploadResource(file, null, () => {})
          upload.current = { file, id: asset.id }
        }
        if (!draft.current) {
          draft.current = await http.post<Form>("/api/light/productions", {
            entry_id: "video",
            brief: product,
          })
        } else {
          draft.current = await http.get<Form>("/api/light/productions/" + draft.current.session_id)
        }
        const form = draft.current
        const updated = await http.patch<Form>("/api/light/productions/" + form.session_id, {
          revision: form.revision,
          reference_example_id: template.example_id,
          product_name: product,
          shop,
          asset_ids: [upload.current.id],
          script,
          promotion_brief: { offer },
          page: "review",
        })
        saved = { form: updated, requestId: crypto.randomUUID() }
        setRun(saved)
      }
      await http.post("/api/light/productions/" + saved.form.session_id + "/actions", {
        revision: saved.form.revision,
        action: "render",
        request_id: saved.requestId,
      })
      close()
      navigate(paths.chat(saved.form.session_id))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <strong>{t("edition", { title: template.title, version: template.template_ref.version })}</strong>
      <fieldset disabled={busy || Boolean(run)} className="flex flex-col gap-2">
        <label>
          {t("photo")}
          <input
            className={field}
            type="file"
            accept="image/*"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        <label>
          {t("product")}
          <input
            className={field}
            maxLength={36}
            value={product}
            onChange={(e) => setProduct(e.target.value)}
          />
        </label>
        <label>
          {t("brand")}
          <input className={field} maxLength={80} value={shop} onChange={(e) => setShop(e.target.value)} />
        </label>
        <label>
          {t("offer")}
          <input className={field} maxLength={160} value={offer} onChange={(e) => setOffer(e.target.value)} />
        </label>
        <label>
          {t("script")}
          <textarea
            className={field}
            value={script}
            maxLength={2400}
            onChange={(e) => setScript(e.target.value)}
          />
        </label>
      </fieldset>
      <button className={primary} disabled={busy || !product.trim() || !file} onClick={() => void start()}>
        {busy ? t("working") : t("generate")}
      </button>
      {error && <p role="alert">{error}</p>}
    </>
  )
}
