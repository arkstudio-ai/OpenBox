// The classification the library files examples under, edited by the team:
// each entry's scenes, the industries and their shop types. What the App and
// the first-video form point at (`builtin`) can be renamed but not removed.
import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { Sheet } from "@/shared/ui/Sheet"
import { useSaveTaxonomy, useTaxonomy } from "./api"
import { fromTaxonomy, taxonomyProblem, toTaxonomyWrite, type TaxonomyForm } from "./lib"
import type { Taxonomy } from "./types"

const FIELD = "border-hair bg-bg text-ink min-h-9 min-w-0 rounded-lg border px-3 text-sm"
const LINK = "text-n600 hover:text-ink text-xs underline-offset-2 hover:underline disabled:opacity-40 disabled:hover:no-underline"
const ADD = "border-hair hover:bg-hairsoft self-start rounded-full border px-3 py-1 text-xs"
const GENERIC = "other"

let serial = 0
const nextKey = () => `new-${++serial}`

function Editor({ taxonomy, onClose, onSaved }: { taxonomy: Taxonomy; onClose: () => void; onSaved?: () => void }) {
  const { t } = useTranslation("admin-examples")
  const id = useId()
  const save = useSaveTaxonomy()
  const [form, setForm] = useState<TaxonomyForm>(() => fromTaxonomy(taxonomy))
  const [error, setError] = useState<string | null>(null)
  const builtinScenes = taxonomy.builtin?.scenes ?? {}
  const builtinIndustries = taxonomy.builtin?.industries ?? []
  const problem = taxonomyProblem(form)

  const setEntry = (entryId: string, change: (scenes: TaxonomyForm["entries"][number]["scenes"]) => TaxonomyForm["entries"][number]["scenes"]) =>
    setForm((prev) => ({ ...prev, entries: prev.entries.map((entry) => (entry.id === entryId ? { ...entry, scenes: change(entry.scenes) } : entry)) }))
  const setIndustry = (key: string, change: (industry: TaxonomyForm["industries"][number]) => TaxonomyForm["industries"][number]) =>
    setForm((prev) => ({ ...prev, industries: prev.industries.map((industry) => (industry.key === key ? change(industry) : industry)) }))

  const submit = async () => {
    if (problem || save.isPending) return
    setError(null)
    try {
      await save.mutateAsync(toTaxonomyWrite(form))
      onSaved?.()
      onClose()
    } catch (reason) {
      if (reason instanceof ApiError) {
        setError(t(`taxonomy.errors.${reason.code}`, { defaultValue: t("taxonomy.errors.default"), message: reason.message }))
      } else {
        setError(t("taxonomy.errors.default"))
      }
    }
  }

  return (
    <>
      <form className="flex flex-col gap-6" onSubmit={(event) => event.preventDefault()}>
        {error && (
          <p role="alert" className="text-dangerink text-sm">
            {error}
          </p>
        )}
        <section className="flex flex-col gap-4" aria-label={t("taxonomy.scenes")}>
          <div>
            <h3 className="text-ink text-sm font-semibold">{t("taxonomy.scenes")}</h3>
            <p className="text-n500 text-2xs">{t("taxonomy.scenesHint")}</p>
          </div>
          {form.entries.map((entry) => (
            <fieldset key={entry.id} className="flex flex-col gap-2" data-testid="taxonomy-entry">
              <legend className="text-n700 mb-1 text-xs font-medium">{entry.label}</legend>
              {entry.scenes.map((scene) => {
                const locked = !!scene.id && (builtinScenes[entry.id] ?? []).includes(scene.id)
                return (
                  <div key={scene.key} className="flex items-center gap-2">
                    <input
                      aria-label={t("taxonomy.sceneName", { entry: entry.label })}
                      className={`${FIELD} flex-1`}
                      maxLength={20}
                      value={scene.label}
                      onChange={(e) => setEntry(entry.id, (scenes) => scenes.map((item) => (item.key === scene.key ? { ...item, label: e.target.value } : item)))}
                    />
                    <button
                      type="button"
                      className={LINK}
                      disabled={locked}
                      title={locked ? t("taxonomy.builtin") : undefined}
                      aria-label={t("taxonomy.removeNamed", { name: scene.label })}
                      onClick={() => setEntry(entry.id, (scenes) => scenes.filter((item) => item.key !== scene.key))}
                    >
                      {locked ? t("taxonomy.builtinTag") : t("form.remove")}
                    </button>
                  </div>
                )
              })}
              <button type="button" className={ADD} onClick={() => setEntry(entry.id, (scenes) => [...scenes, { key: nextKey(), id: null, label: "" }])}>
                {t("taxonomy.addScene")}
              </button>
            </fieldset>
          ))}
        </section>

        <section className="flex flex-col gap-4" aria-label={t("taxonomy.industries")}>
          <div>
            <h3 className="text-ink text-sm font-semibold">{t("taxonomy.industries")}</h3>
            <p className="text-n500 text-2xs">{t("taxonomy.industriesHint")}</p>
          </div>
          {form.industries.map((industry) => {
            const locked = !!industry.id && builtinIndustries.includes(industry.id)
            return (
              <fieldset key={industry.key} className="border-hair flex flex-col gap-2 rounded-lg border p-3" data-testid="taxonomy-industry">
                <div className="flex items-center gap-2">
                  <input
                    aria-label={t("taxonomy.industryName")}
                    className={`${FIELD} flex-1 font-medium`}
                    maxLength={20}
                    value={industry.label}
                    onChange={(e) => setIndustry(industry.key, (item) => ({ ...item, label: e.target.value }))}
                  />
                  <button
                    type="button"
                    className={LINK}
                    disabled={locked}
                    title={locked ? t("taxonomy.builtin") : undefined}
                    aria-label={t("taxonomy.removeNamed", { name: industry.label })}
                    onClick={() => setForm((prev) => ({ ...prev, industries: prev.industries.filter((item) => item.key !== industry.key) }))}
                  >
                    {locked ? t("taxonomy.builtinTag") : t("form.remove")}
                  </button>
                </div>
                {industry.shop_types.map((shop) => {
                  const generic = shop.id === GENERIC
                  const patchShop = (changes: Partial<typeof shop>) =>
                    setIndustry(industry.key, (item) => ({ ...item, shop_types: item.shop_types.map((other) => (other.key === shop.key ? { ...other, ...changes } : other)) }))
                  return (
                    <div key={shop.key} className="flex flex-wrap items-center gap-2 pl-3">
                      <input aria-label={t("taxonomy.shopName", { industry: industry.label })} className={`${FIELD} w-40`} maxLength={20} value={shop.label} onChange={(e) => patchShop({ label: e.target.value })} />
                      <input
                        aria-label={t("taxonomy.keywordsOf", { name: shop.label })}
                        className={`${FIELD} flex-1 basis-40`}
                        placeholder={t("taxonomy.keywords")}
                        value={shop.keywords}
                        onChange={(e) => patchShop({ keywords: e.target.value })}
                      />
                      <button
                        type="button"
                        className={LINK}
                        disabled={generic}
                        title={generic ? t("taxonomy.generic") : undefined}
                        aria-label={t("taxonomy.removeNamed", { name: shop.label })}
                        onClick={() => setIndustry(industry.key, (item) => ({ ...item, shop_types: item.shop_types.filter((other) => other.key !== shop.key) }))}
                      >
                        {generic ? t("taxonomy.builtinTag") : t("form.remove")}
                      </button>
                    </div>
                  )
                })}
                <button
                  type="button"
                  className={`${ADD} ml-3`}
                  onClick={() =>
                    setIndustry(industry.key, (item) => {
                      // New ones go above the generic type, which stays last.
                      const at = item.shop_types.findIndex((shop) => shop.id === GENERIC)
                      const added = { key: nextKey(), id: null, label: "", keywords: "" }
                      const list = [...item.shop_types]
                      list.splice(at < 0 ? list.length : at, 0, added)
                      return { ...item, shop_types: list }
                    })
                  }
                >
                  {t("taxonomy.addShop")}
                </button>
              </fieldset>
            )
          })}
          <button
            type="button"
            className={ADD}
            onClick={() =>
              setForm((prev) => ({
                ...prev,
                industries: [...prev.industries, { key: nextKey(), id: null, label: "", shop_types: [{ key: nextKey(), id: GENERIC, label: t("taxonomy.genericLabel"), keywords: "" }] }],
              }))
            }
          >
            {t("taxonomy.addIndustry")}
          </button>
        </section>
      </form>
      <div className="border-hair mt-4 flex items-center justify-between gap-3 border-t pt-3">
        <span className="text-n500 text-xs" id={`${id}-problem`}>
          {problem ? t(`taxonomy.problem.${problem}`) : ""}
        </span>
        <button type="button" disabled={save.isPending || !!problem} onClick={() => void submit()} className="bg-ink text-bg rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50">
          {save.isPending ? t("form.saving") : t("form.save")}
        </button>
      </div>
    </>
  )
}

export function TaxonomySheet({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved?: () => void }) {
  const { t } = useTranslation("admin-examples")
  const taxonomy = useTaxonomy()
  return (
    <Sheet open={open} onClose={onClose} title={t("taxonomy.title")} description={t("taxonomy.description")} closeLabel={t("form.close")} dismissOnBackdrop={false}>
      {open && taxonomy.data && <Editor key={taxonomy.data.version} taxonomy={taxonomy.data} onClose={onClose} onSaved={onSaved} />}
    </Sheet>
  )
}
