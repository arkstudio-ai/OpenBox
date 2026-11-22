// One sheet for the three ways an example gets written: collected from a
// session (the server prefilled it; the team adds shop type, tags, rating and
// notes), made by hand from existing asset ids, or edited afterwards.
import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { Sheet } from "@/shared/ui/Sheet"
import { Spinner } from "@/shared/ui/Spinner"
import { useQuery } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { exampleKeys, prefillExample, useAssetLink, useExample, useReadCard, useSaveExample, useTaxonomy } from "./api"
import { EMPTY, MAX_ASSETS, MAX_CARD, MAX_RECIPE, scenesOf, toWrite, type FormState } from "./lib"
import { ENTRY_IDS, type AssetRef, type Example, type ExampleDraft, type ExampleWrite, type Scene, type Taxonomy } from "./types"

export type SheetMode =
  | { kind: "collect"; sessionId: string; assetId?: string | null }
  | { kind: "new" }
  | { kind: "edit"; example: Example }

const FIELD = "border-hair bg-bg text-ink min-h-9 w-full rounded-lg border px-3 text-sm"
const AREA = "border-hair bg-bg text-ink w-full rounded-lg border px-3 py-2 text-sm leading-relaxed"

function fromDraft(draft: ExampleDraft): FormState {
  return {
    ...EMPTY,
    entry_id: draft.entry_id ?? "video",
    industry: draft.industry || "food",
    shop_type: draft.shop_type || "other",
    subtype: draft.subtype ?? "",
    scene: draft.scene ?? "",
    title: draft.title,
    recipe: draft.recipe,
    inputs: draft.inputs,
    outputs: draft.outputs,
    source: draft.source,
  }
}

function fromExample(example: Example): FormState {
  return {
    ...EMPTY,
    entry_id: example.entry_id,
    industry: example.industry,
    shop_type: example.shop_type,
    subtype: example.subtype ?? "",
    scene: example.scene ?? "",
    title: example.title,
    tags: example.tags.join(", "),
    rating: example.rating,
    notes: example.notes,
    recipe: example.recipe,
    inputs: example.inputs,
    outputs: example.outputs,
    source: example.source,
    status: example.status,
  }
}

/** Image and video outputs show in place through the admin link; anything else is a name. */
export function AssetTile({ asset }: { asset: AssetRef }) {
  const { t } = useTranslation("admin-examples")
  const link = useAssetLink(asset.asset_id)
  const url = link.data?.url
  const mime = asset.mime || link.data?.mime || ""
  const name = asset.name || link.data?.name || asset.asset_id
  if (!url) {
    return (
      <div className="text-n600 truncate text-xs" data-testid="asset-tile">
        {name}
        {link.isError && ` · ${t("preview.unavailable")}`}
      </div>
    )
  }
  return (
    <figure className="flex flex-col gap-1" data-testid="asset-tile">
      {mime.startsWith("video/") ? (
        <video src={url} controls muted playsInline preload="metadata" className="border-hair max-h-56 w-full rounded-lg border bg-black" />
      ) : mime.startsWith("image/") ? (
        <img src={url} alt={name} className="border-hair max-h-56 w-full rounded-lg border object-contain" />
      ) : null}
      <figcaption className="truncate">
        <a href={url} target="_blank" rel="noreferrer" className="text-n600 hover:text-ink text-xs underline-offset-2 hover:underline">
          {name}
        </a>
      </figcaption>
    </figure>
  )
}

function RemovableTile({ asset, onRemove }: { asset: AssetRef; onRemove: () => void }) {
  const { t } = useTranslation("admin-examples")
  return (
    <div className="flex flex-col gap-1">
      <AssetTile asset={asset} />
      <button
        type="button"
        onClick={onRemove}
        aria-label={t("form.removeAsset", { name: asset.name || asset.asset_id })}
        className="text-n600 hover:text-ink self-start text-xs underline-offset-2 hover:underline"
      >
        {t("form.remove")}
      </button>
    </div>
  )
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-n700 text-xs font-medium">
        {label}
      </label>
      {children}
      {hint && <p className="text-n500 text-2xs">{hint}</p>}
    </div>
  )
}

function shopTypesOf(taxonomy: Taxonomy | undefined, industry: string) {
  return taxonomy?.industries.find((item) => item.id === industry)?.shop_types ?? []
}

/** A scene belongs to one entry: it is kept only where that entry lists it (or while the taxonomy is unread). */
function sceneIn(taxonomy: Taxonomy | undefined, entryId: string, scene: string): string {
  return !taxonomy || scenesOf(taxonomy, entryId).some((item) => item.id === scene) ? scene : ""
}

/** The entry's scenes; an entry that is not split by scene keeps the select in place but unusable. */
function SceneField({ id, scenes, value, onChange }: { id: string; scenes: Scene[]; value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation("admin-examples")
  const options = scenes.length || !value ? scenes : [{ id: value, label: value }]
  return (
    <Field id={id} label={t("form.scene")} hint={scenes.length ? undefined : t("form.sceneUnused")}>
      <select id={id} className={`${FIELD} disabled:opacity-50`} value={value} disabled={!scenes.length} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t("form.sceneNone")}</option>
        {options.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
          </option>
        ))}
      </select>
    </Field>
  )
}

/**
 * The reference card of a saved example: what a model wrote after looking at the piece, its state, and a way
 * to have it written again. Until the team types in it the texts follow the server; after that they are theirs.
 */
function CardSection({ example, card, onEdit, onReset }: { example: Example; card: string | null; onEdit: (card: string) => void; onReset: () => void }) {
  const { t } = useTranslation("admin-examples")
  const id = useId()
  const fetched = useExample(example.id).data
  const live = fetched?.id === example.id ? fetched : example
  const read = useReadCard()
  const state = read.isPending ? "reading" : (live.card_state?.state ?? "none")
  const shownCard = card ?? live.card
  const detail = [live.card_state?.model, live.card_state?.frames ? t("card.frames", { count: live.card_state.frames }) : null, live.card_state?.kind === "video" ? (live.card_state.heard ? t("card.heard") : t("card.silent")) : null].filter(Boolean).join(" · ")
  return (
    <section className="border-hair flex flex-col gap-2 rounded-lg border p-3" aria-label={t("card.title")} data-testid="example-card">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-n700 text-xs font-medium">{t("card.title")}</h3>
        <button
          type="button"
          disabled={state === "reading"}
          onClick={() => {
            onReset()
            read.mutate(example.id)
          }}
          className="text-n600 hover:text-ink text-xs underline-offset-2 hover:underline disabled:opacity-50"
        >
          {t("card.read")}
        </button>
      </div>
      <p className={`text-xs ${state === "failed" || read.isError ? "text-dangerink" : "text-n500"}`} data-testid="example-card-state">
        {state === "reading" && <Spinner className="mr-1 inline size-3" />}
        {read.isError ? t("card.state.failed") : t(`card.state.${card !== null ? "editing" : state}`)}
        {state === "ready" && card === null && detail ? ` · ${detail}` : ""}
      </p>
      <Field id={`${id}-card`} label={t("card.full")} hint={t("card.fullHint")}>
        <textarea id={`${id}-card`} className={AREA} rows={12} maxLength={MAX_CARD} value={shownCard} disabled={state === "reading"} onChange={(e) => onEdit(e.target.value)} />
      </Field>
      {live.card_state?.kind === "video" &&
        (live.card_state.edit_lines?.length ? (
          <details className="text-xs" data-testid="example-card-edit">
            <summary className="text-n600 hover:text-ink cursor-pointer">{t("card.edit", { count: live.card_state.edit_lines.length })}</summary>
            <pre className="text-n600 mt-2 font-sans text-xs leading-5 whitespace-pre-wrap">{live.card_state.edit_lines.join("\n")}</pre>
          </details>
        ) : (
          state === "ready" && <p className="text-n500 text-xs">{t("card.noEdit")}</p>
        ))}
    </section>
  )
}

function modeKey(mode: SheetMode): string {
  if (mode.kind === "edit") return `edit:${mode.example.id}:${mode.example.updated_at ?? ""}`
  if (mode.kind === "collect") return `collect:${mode.sessionId}:${mode.assetId ?? ""}`
  return "new"
}

/** What stops this form from saving: the same refusals the server would give, said where they can be fixed. */
function useFormProblems(write: ExampleWrite) {
  const { t } = useTranslation("admin-examples")
  const tooMany = (count: number, kind: string) => (count > MAX_ASSETS ? t("form.tooManyAssets", { kind, count, max: MAX_ASSETS }) : null)
  const outputsOver = tooMany(write.outputs.length, t("form.outputs"))
  const inputsOver = tooMany(write.inputs.length, t("form.inputs"))
  const recipeOver = write.recipe.length > MAX_RECIPE ? t("form.recipeTooLong", { count: write.recipe.length, max: MAX_RECIPE }) : null
  const missing = !write.title ? t("form.needTitle") : !write.outputs.length ? t("form.needOutput") : null
  return { outputsOver, inputsOver, problem: missing ?? outputsOver ?? inputsOver ?? recipeOver }
}

function SheetForm({
  mode,
  initial,
  taxonomy,
  onClose,
  onSaved,
}: {
  mode: SheetMode
  initial: FormState
  taxonomy: Taxonomy | undefined
  onClose: () => void
  onSaved?: (example: Example) => void
}) {
  const { t } = useTranslation("admin-examples")
  const id = useId()
  const save = useSaveExample()
  const [state, setState] = useState<FormState>(initial)
  const [error, setError] = useState<string | null>(null)
  const patch = (changes: Partial<FormState>) => setState((prev) => ({ ...prev, ...changes }))

  const shopTypes = shopTypesOf(taxonomy, state.industry)
  const scenes = scenesOf(taxonomy, state.entry_id)
  const scene = sceneIn(taxonomy, state.entry_id, state.scene)
  const write = toWrite({ ...state, scene })
  const { outputsOver, inputsOver, problem } = useFormProblems(write)
  const removeOutput = (assetId: string) => patch({ outputs: state.outputs.filter((asset) => asset.asset_id !== assetId) })
  const removeInput = (assetId: string) => patch({ inputs: state.inputs.filter((asset) => asset.asset_id !== assetId) })

  const submit = async () => {
    if (problem || save.isPending) return
    setError(null)
    try {
      const saved = await save.mutateAsync({ id: mode.kind === "edit" ? mode.example.id : null, body: write })
      onSaved?.(saved)
      onClose()
    } catch (reason) {
      if (reason instanceof ApiError) {
        setError(t(`form.errors.${reason.code}`, { defaultValue: t("form.errors.default"), message: reason.message }))
      } else {
        setError(t("form.errors.default"))
      }
    }
  }

  return (
    <>
      <form className="flex flex-col gap-4" onSubmit={(event) => event.preventDefault()}>
        {error && (
          <p role="alert" className="text-dangerink text-sm">
            {error}
          </p>
        )}
        {(state.source.session_id || state.source.shop_name) && (
          <p className="text-n600 text-xs" data-testid="example-source">
            {t("form.source")}：
            {t("form.sourceLine", {
              session: state.source.session_id ?? "—",
              shop: state.source.shop_name || "—",
              headline: state.source.headline || "—",
            })}
          </p>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field id={`${id}-entry`} label={t("form.entry")}>
            <select
              id={`${id}-entry`}
              className={FIELD}
              value={state.entry_id}
              // A scene belongs to one entry; it survives the change only when the new entry has it too.
              onChange={(e) => patch({ entry_id: e.target.value, scene: sceneIn(taxonomy, e.target.value, scene) })}
            >
              {(taxonomy?.entries ?? ENTRY_IDS.map((entry) => ({ id: entry, label: entry }))).map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
          </Field>
          <SceneField id={`${id}-scene`} scenes={scenes} value={scene} onChange={(value) => patch({ scene: value })} />
          <Field id={`${id}-industry`} label={t("form.industry")}>
            <select
              id={`${id}-industry`}
              className={FIELD}
              value={state.industry}
              // A shop type belongs to one industry; a new industry starts from its generic one.
              onChange={(e) => patch({ industry: e.target.value, shop_type: "other" })}
            >
              {(taxonomy?.industries ?? [{ id: state.industry, label: state.industry }]).map((industry) => (
                <option key={industry.id} value={industry.id}>
                  {industry.label}
                </option>
              ))}
            </select>
          </Field>
          <Field id={`${id}-shop`} label={t("form.shopType")}>
            <select id={`${id}-shop`} className={FIELD} value={state.shop_type} onChange={(e) => patch({ shop_type: e.target.value })}>
              {(shopTypes.length ? shopTypes : [{ id: state.shop_type, label: state.shop_type }]).map((shop) => (
                <option key={shop.id} value={shop.id}>
                  {shop.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field id={`${id}-title`} label={t("form.title")}>
          <input id={`${id}-title`} className={FIELD} maxLength={120} value={state.title} onChange={(e) => patch({ title: e.target.value })} data-autofocus />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="sm:col-span-2">
            <Field id={`${id}-tags`} label={t("form.tags")} hint={t("form.tagsHint")}>
              <input id={`${id}-tags`} className={FIELD} value={state.tags} onChange={(e) => patch({ tags: e.target.value })} />
            </Field>
          </div>
          <Field id={`${id}-rating`} label={t("form.rating")} hint={t("form.ratingHint")}>
            <select id={`${id}-rating`} className={FIELD} value={state.rating} onChange={(e) => patch({ rating: Number(e.target.value) })}>
              {[5, 4, 3, 2, 1].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field id={`${id}-subtype`} label={t("form.subtype")}>
          <input id={`${id}-subtype`} className={FIELD} maxLength={64} placeholder={t("form.subtypePlaceholder")} value={state.subtype} onChange={(e) => patch({ subtype: e.target.value })} />
        </Field>
        <Field id={`${id}-notes`} label={t("form.notes")} hint={t("form.notesHint")}>
          <textarea id={`${id}-notes`} className={AREA} rows={3} maxLength={2000} value={state.notes} onChange={(e) => patch({ notes: e.target.value })} />
        </Field>
        <Field id={`${id}-recipe`} label={t("form.recipe")}>
          <textarea id={`${id}-recipe`} className={`${AREA} font-mono text-xs`} rows={8} maxLength={MAX_RECIPE} value={state.recipe} onChange={(e) => patch({ recipe: e.target.value })} />
        </Field>
        {mode.kind === "edit" ? (
          <CardSection example={mode.example} card={state.card} onEdit={(card) => patch({ card })} onReset={() => patch({ card: null })} />
        ) : (
          <p className="text-n500 text-xs">{t("card.afterSave")}</p>
        )}
        <section className="flex flex-col gap-2" aria-label={t("form.outputs")}>
          <h3 className="text-n700 text-xs font-medium">{t("form.outputs")}</h3>
          {outputsOver && <p className="text-dangerink text-xs">{outputsOver}</p>}
          {state.outputs.length ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {state.outputs.map((asset) => (
                <RemovableTile key={asset.asset_id} asset={asset} onRemove={() => removeOutput(asset.asset_id)} />
              ))}
            </div>
          ) : (
            mode.kind === "collect" && <p className="text-n500 text-xs">{t("form.noAssets")}</p>
          )}
          {mode.kind !== "collect" && (
            <textarea
              aria-label={`${t("form.outputs")} ${t("form.assetIds")}`}
              className={`${AREA} font-mono text-xs`}
              rows={2}
              placeholder={t("form.assetIds")}
              value={state.outputIds}
              onChange={(e) => patch({ outputIds: e.target.value })}
            />
          )}
        </section>
        <section className="flex flex-col gap-2" aria-label={t("form.inputs")}>
          <h3 className="text-n700 text-xs font-medium">{t("form.inputs")}</h3>
          {inputsOver && <p className="text-dangerink text-xs">{inputsOver}</p>}
          {state.inputs.length > 0 && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {state.inputs.map((asset) => (
                <RemovableTile key={asset.asset_id} asset={asset} onRemove={() => removeInput(asset.asset_id)} />
              ))}
            </div>
          )}
          {mode.kind !== "collect" && (
            <textarea
              aria-label={`${t("form.inputs")} ${t("form.assetIds")}`}
              className={`${AREA} font-mono text-xs`}
              rows={2}
              placeholder={t("form.assetIds")}
              value={state.inputIds}
              onChange={(e) => patch({ inputIds: e.target.value })}
            />
          )}
        </section>
      </form>
      <div className="border-hair mt-4 flex items-center justify-between gap-3 border-t pt-3">
        <span className="text-n500 text-xs">{problem ?? ""}</span>
        <button
          type="button"
          disabled={save.isPending || !!problem}
          onClick={() => void submit()}
          className="bg-ink text-bg rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {save.isPending ? t("form.saving") : t("form.save")}
        </button>
      </div>
    </>
  )
}

export function ExampleSheet({ mode, onClose, onSaved }: { mode: SheetMode | null; onClose: () => void; onSaved?: (example: Example) => void }) {
  const { t } = useTranslation("admin-examples")
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const taxonomy = useTaxonomy()
  const collect = mode?.kind === "collect" ? mode : null
  // The session is read once per (session, asset); the form then starts from it.
  const draft = useQuery({
    queryKey: [...exampleKeys.all(userId), "prefill", collect?.sessionId ?? "", collect?.assetId ?? ""] as const,
    queryFn: () => prefillExample(collect!.sessionId, collect!.assetId),
    enabled: collect !== null,
    staleTime: Infinity,
    retry: false,
  })
  const title = mode?.kind === "edit" ? t("form.editTitle") : mode?.kind === "new" ? t("form.newTitle") : t("form.collectTitle")
  const description = mode?.kind === "collect" ? t("form.collectDescription") : mode?.kind === "new" ? t("form.newDescription") : undefined

  let body: React.ReactNode = null
  if (mode) {
    if (mode.kind === "collect" && draft.isPending) {
      body = (
        <div className="flex items-center gap-2 py-8">
          <Spinner className="size-4" />
          <span className="text-n600 text-sm">{t("form.loading")}</span>
        </div>
      )
    } else if (mode.kind === "collect" && draft.isError) {
      const reason = draft.error
      body = (
        <p role="alert" className="text-dangerink py-8 text-sm">
          {reason instanceof ApiError ? t(`form.errors.${reason.code}`, { defaultValue: t("form.loadFailed") }) : t("form.loadFailed")}
        </p>
      )
    } else {
      const initial = mode.kind === "edit" ? fromExample(mode.example) : mode.kind === "collect" ? fromDraft(draft.data!) : EMPTY
      body = <SheetForm key={modeKey(mode)} mode={mode} initial={initial} taxonomy={taxonomy.data} onClose={onClose} onSaved={onSaved} />
    }
  }

  return (
    <Sheet open={mode !== null} onClose={onClose} title={title} description={description} closeLabel={t("form.close")} dismissOnBackdrop={false}>
      {body}
    </Sheet>
  )
}
