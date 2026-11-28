import { useRef, useState, type FormEvent } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"
import { completeAsset, createAsset, putToOss } from "@/features/chat"
import { http } from "@/shared/api/http"
import { useAssetUrl } from "@/shared/api/assets"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { MEDIA, useMediaScope, useProduction, type Dish, type Production, type Template } from "./api"
import { button, field, Preview, primary, useMediaText } from "./shared"

interface DraftInputs {
  shop: string
  script: string
  productName: string
  headline: string
  referenceCopy: string
  dishes: Dish[]
  keepOriginal: boolean
  perfectFilm: boolean
  language: string
  assetIds: string[]
}

function Result({ output }: { output: NonNullable<Production["output"]> }) {
  const link = useAssetUrl(output.id)
  const { tx } = useMediaText()
  if (link.isError)
    return (
      <button className={button} onClick={() => void link.refetch()}>
        {tx("retry")}
      </button>
    )
  return <Preview media={{ name: output.name, mime: output.mime, media_url: link.data?.url }} />
}

function ProductionForm({ task, save }: { task: Production; save: (task: Production) => void }) {
  const { tx } = useMediaText()
  const errorMessage = useApiErrorMessage()
  const [inputs, setInputs] = useState<DraftInputs>({
    shop: task.shop,
    script: task.script,
    productName: task.product_name ?? "",
    headline: task.poster_title ?? "",
    referenceCopy: task.reference_copy,
    dishes: task.dishes.length ? task.dishes : [{ name: "", price: "" }],
    keepOriginal: task.keep_original,
    perfectFilm: task.perfect_film === true,
    language: task.output_language ?? "",
    assetIds: task.materials.map((asset) => asset.id),
  })
  const [files, setFiles] = useState<File[]>([])
  const video = task.entry_id === "video"
  const footage = task.entry_id === "footage_edit"
  const poster = task.entry_id === "poster"
  const menu = task.entry_id === "menu"
  const builtin = task.reference_example_id?.startsWith("mtb_") ?? false
  const referenceMode = builtin || task.reference_example_id?.startsWith("vt_")
  const [confirmQuality, setConfirmQuality] = useState(false)
  const lastRequest = useRef<{ revision: number; action: string; id: string }>(undefined)
  const gallery = task.template === "gallery_wishes"
  const requiresPlan = builtin && !task.builtin_h3 && ["shortcut_story", "silk_road"].includes(task.template)
  const [prepared, setPrepared] = useState(task.remake_prepared === true || task.builtin_h3 === true)
  const [notice, setNotice] = useState("")
  const [current, setCurrent] = useState(task)
  const url = `/api/light/productions/${task.session_id}`
  const run = useMutation({
    mutationFn: async (action: "prepare" | "render" | "save") => {
      const total = inputs.assetIds.length + files.length
      if (total > 10) throw new Error(tx("tooManyFiles"))
      const ids = [...inputs.assetIds]
      for (const file of files) {
        const ticket = await createAsset(file.name, file.type, file.size, task.session_id)
        await putToOss(ticket, file, () => {})
        ids.push((await completeAsset(ticket.id)).id)
      }
      const saved = await http.patch<Production>(url, {
        revision: current.revision,
        asset_ids: ids,
        shop: inputs.shop,
        ...(menu ? { dishes: inputs.dishes.filter((dish) => dish.name.trim()) } : { script: inputs.script }),
        ...(video
          ? {
              product_name: inputs.productName,
              reference_copy: inputs.referenceCopy,
              output_language: inputs.language,
              perfect_film: inputs.perfectFilm,
            }
          : {}),
        ...(poster ? { poster_title: inputs.headline } : {}),
        ...(footage ? { keep_original: inputs.keepOriginal } : {}),
      })
      setCurrent(saved)
      setInputs((value) => ({ ...value, assetIds: ids }))
      setFiles([])
      if (action === "save") return saved
      let request = lastRequest.current
      if (!request || request.action !== action || request.revision !== saved.revision) {
        request = { revision: saved.revision, action, id: crypto.randomUUID() }
        lastRequest.current = request
      }
      return http.post<Production>(`${url}/actions`, {
        action,
        revision: saved.revision,
        request_id: request.id,
      })
    },
    onError: async () => {
      const latest = await http.get<Production>(url).catch(() => undefined)
      if (!latest) return
      setCurrent(latest)
      if (["working", "blocked", "completed", "review"].includes(latest.status)) save(latest)
    },
    onSuccess: (saved, action) => {
      save(saved)
      if (action === "save") setPrepared(saved.remake_prepared === true || saved.builtin_h3 === true)
      setNotice(action === "save" ? tx("saved") : "")
    },
  })
  const update = <K extends keyof DraftInputs>(key: K, value: DraftInputs[K]) => {
    setInputs((previous) => ({ ...previous, [key]: value }))
    setPrepared(false)
  }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setNotice("")
    run.mutate(requiresPlan && !prepared ? "prepare" : "render")
  }
  return (
    <form onSubmit={submit} className="grid gap-3">
      <label className="grid gap-1 text-sm">
        {tx("shop")}
        <input
          className={field}
          value={inputs.shop}
          required={gallery}
          maxLength={80}
          onChange={(event) => update("shop", event.target.value)}
        />
      </label>
      {video && (
        <label className="grid gap-1 text-sm">
          {tx("product")}
          <input
            className={field}
            value={inputs.productName}
            required={gallery}
            maxLength={36}
            onChange={(event) => update("productName", event.target.value)}
          />
        </label>
      )}
      {poster && (
        <label className="grid gap-1 text-sm">
          {tx("headline")}
          <input
            className={field}
            value={inputs.headline}
            required
            maxLength={120}
            onChange={(event) => update("headline", event.target.value)}
          />
        </label>
      )}
      {!menu && (
        <label className="grid gap-1 text-sm">
          {tx("brief")}
          <textarea
            className={field}
            rows={4}
            value={referenceMode ? inputs.referenceCopy : inputs.script}
            maxLength={2400}
            required={video && !referenceMode}
            onChange={(event) => update(referenceMode ? "referenceCopy" : "script", event.target.value)}
          />
        </label>
      )}
      {menu && (
        <fieldset className="grid gap-2">
          <legend className="mb-1 text-sm">{tx("dishes")}</legend>
          {inputs.dishes.map((dish, index) => (
            <div className="grid grid-cols-[1fr_8rem_auto] gap-2" key={index}>
              {(["name", "price"] as const).map((key) => (
                <input
                  key={key}
                  aria-label={tx(key)}
                  placeholder={tx(key)}
                  className={field}
                  value={dish[key]}
                  maxLength={key === "name" ? 100 : 32}
                  required={key === "name" && index === 0}
                  onChange={(event) =>
                    update(
                      "dishes",
                      inputs.dishes.map((row, i) =>
                        i === index ? { ...row, [key]: event.target.value } : row,
                      ),
                    )
                  }
                />
              ))}
              <button
                type="button"
                className={button}
                disabled={inputs.dishes.length < 2}
                onClick={() =>
                  update(
                    "dishes",
                    inputs.dishes.filter((_, i) => i !== index),
                  )
                }
              >
                {tx("remove")}
              </button>
            </div>
          ))}
          <button
            type="button"
            className={button}
            disabled={inputs.dishes.length >= 30}
            onClick={() => update("dishes", [...inputs.dishes, { name: "", price: "" }])}
          >
            {tx("addDish")}
          </button>
        </fieldset>
      )}
      <label className="grid gap-1 text-sm">
        {tx("materials")}
        <input
          className={field}
          type="file"
          multiple={!gallery}
          accept={menu || poster || gallery ? "image/*" : "image/*,video/*"}
          required={inputs.assetIds.length === 0 && (footage || gallery || requiresPlan)}
          onChange={(event) => {
            setFiles(Array.from(event.target.files ?? []))
            setPrepared(false)
          }}
        />
      </label>
      {current.materials
        .filter((asset) => inputs.assetIds.includes(asset.id))
        .map((asset) => (
          <div key={asset.id} className="text-n700 flex items-center justify-between gap-2 text-sm">
            <span className="truncate">{asset.name}</span>
            <button
              type="button"
              className={button}
              onClick={() =>
                update(
                  "assetIds",
                  inputs.assetIds.filter((id) => id !== asset.id),
                )
              }
            >
              {tx("remove")}
            </button>
          </div>
        ))}
      {footage && (
        <label className="flex gap-2 text-sm">
          <input
            type="checkbox"
            checked={inputs.keepOriginal}
            onChange={(event) => update("keepOriginal", event.target.checked)}
          />
          {tx("keepOriginal")}
        </label>
      )}
      {video && (
        <>
          <label className="grid gap-1 text-sm">
            {tx("language")}
            <input
              className={field}
              value={inputs.language}
              placeholder={tx("automaticLanguage")}
              maxLength={80}
              onChange={(event) => update("language", event.target.value)}
            />
          </label>
          <label className="flex gap-2 text-sm">
            <input
              type="checkbox"
              checked={inputs.perfectFilm}
              onChange={(event) =>
                event.target.checked ? setConfirmQuality(true) : update("perfectFilm", false)
              }
            />
            {tx("perfectFilm")}
          </label>
          {confirmQuality && (
            <Dialog open onClose={() => setConfirmQuality(false)} label={tx("perfectFilm")}>
              <DialogTitle>{tx("perfectFilm")}</DialogTitle>
              <DialogBody>{tx("perfectFilmNote")}</DialogBody>
              <DialogActions>
                <button className={button} type="button" onClick={() => setConfirmQuality(false)}>
                  {tx("cancel")}
                </button>
                <button
                  className={primary}
                  type="button"
                  onClick={() => {
                    update("perfectFilm", true)
                    setConfirmQuality(false)
                  }}
                >
                  {tx("enable")}
                </button>
              </DialogActions>
            </Dialog>
          )}
        </>
      )}
      {run.error && (
        <p role="alert" className="text-dangerink text-sm">
          {errorMessage(run.error)}
        </p>
      )}
      {notice && (
        <p role="status" className="text-n700 text-sm">
          {notice}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button className={button} type="button" disabled={run.isPending} onClick={() => run.mutate("save")}>
          {tx("save")}
        </button>
        <button className={primary} type="submit" disabled={run.isPending}>
          {tx(
            run.isPending
              ? "saving"
              : requiresPlan && !prepared
                ? "prepare"
                : task.status === "failed"
                  ? "retryRender"
                  : "render",
          )}
        </button>
      </div>
    </form>
  )
}

export function GuidedProduction({
  sessionId,
  edition,
  confirmed,
  onConfirm,
  onClose,
}: {
  sessionId: string
  edition?: Template
  confirmed?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const { tx } = useMediaText()
  const scope = useMediaScope()
  const cache = useQueryClient()
  const get = useProduction(sessionId)
  const errorMessage = useApiErrorMessage()
  const confirm = useMutation({
    mutationFn: (outputId: string) =>
      http.post(`${MEDIA}/${edition?.id}/confirm`, { session_id: sessionId, output_asset_id: outputId }),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: [...scope, "mine"] })
      onConfirm()
    },
  })
  const task = get.data
  const working = task && ["working", "blocked", "review"].includes(task.status)
  return (
    <Dialog open onClose={onClose} wide label={tx("guided")}>
      <DialogTitle>{tx(edition ? "trialTitle" : "guided")}</DialogTitle>
      {task && (
        <DialogBody>{task.reference_example_id ? (edition?.title ?? task.title) : task.title}</DialogBody>
      )}
      {get.isPending && <p role="status">{tx("loading")}</p>}
      {get.error && (
        <p role="alert" className="text-dangerink">
          {errorMessage(get.error)}
        </p>
      )}
      {task?.status === "completed" && task.output ? (
        <>
          <Result output={task.output} />
          {confirm.error && (
            <p role="alert" className="text-dangerink">
              {errorMessage(confirm.error)}
            </p>
          )}
          <DialogActions>
            {edition ? (
              <button
                className={primary}
                disabled={confirmed || confirm.isPending}
                onClick={() => confirm.mutate(task.output!.id)}
              >
                {tx(confirmed ? "confirmed" : "confirmTrial")}
              </button>
            ) : (
              <Link className={button} to={paths.chat(task.session_id)}>
                {tx("openConversation")}
              </Link>
            )}
          </DialogActions>
        </>
      ) : working ? (
        <>
          <p role="status">{tx(task.status === "review" ? "awaiting" : "working")}</p>
          <DialogActions>
            <button className={button} onClick={() => void get.refetch()}>
              {tx("refresh")}
            </button>
            <Link className={button} to={paths.chat(task.session_id)}>
              {tx("openConversation")}
            </Link>
          </DialogActions>
        </>
      ) : task ? (
        <ProductionForm
          key={task.session_id}
          task={task}
          save={(value) => cache.setQueryData([...scope, "production", sessionId], value)}
        />
      ) : null}
    </Dialog>
  )
}
