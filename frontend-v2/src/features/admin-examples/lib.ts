// Form state and the pure conversions behind the example sheet, kept out of
// the component file so fast refresh keeps working.
import type { AssetRef, ExampleWrite, Scene, Taxonomy, TaxonomyWrite } from "./types"

export interface FormState {
  entry_id: string
  industry: string
  shop_type: string
  subtype: string
  scene: string
  title: string
  tags: string
  rating: number
  notes: string
  recipe: string
  /** null until the team edits the reference card; then the text is theirs. */
  card: string | null
  inputs: AssetRef[]
  outputs: AssetRef[]
  inputIds: string
  outputIds: string
  source: ExampleWrite["source"]
  status: ExampleWrite["status"]
}

/** The same limits the server's ExampleIn enforces; a session can bring back more than these. */
export const MAX_ASSETS = 12
export const MAX_RECIPE = 20000
export const MAX_CARD = 2400

export const EMPTY: FormState = {
  entry_id: "video",
  industry: "food",
  shop_type: "other",
  subtype: "",
  scene: "",
  title: "",
  tags: "",
  rating: 4,
  notes: "",
  recipe: "",
  card: null,
  inputs: [],
  outputs: [],
  inputIds: "",
  outputIds: "",
  source: {},
  status: "active",
}

/** The scenes one entry is split into; none for an entry without them or before the taxonomy is read. */
export function scenesOf(taxonomy: Taxonomy | undefined, entryId: string): Scene[] {
  return taxonomy?.entries.find((entry) => entry.id === entryId)?.scenes ?? []
}

export function splitTags(text: string): string[] {
  const seen: string[] = []
  for (const raw of text.split(/[,，\n]/)) {
    const tag = raw.trim().slice(0, 20)
    if (tag && !seen.includes(tag)) seen.push(tag)
  }
  return seen.slice(0, 12)
}

function splitIds(text: string): string[] {
  return text
    .split(/[\s,，]+/)
    .map((id) => id.trim())
    .filter(Boolean)
}

/** A listed asset becomes its id line; ids typed by hand stay as typed. */
function assetIds(listed: AssetRef[], typed: string): { asset_id: string }[] {
  const ids = [...listed.map((asset) => asset.asset_id), ...splitIds(typed)]
  return Array.from(new Set(ids)).map((asset_id) => ({ asset_id }))
}

export function toWrite(state: FormState): ExampleWrite {
  return {
    entry_id: state.entry_id,
    industry: state.industry,
    shop_type: state.shop_type,
    subtype: state.subtype.trim() || null,
    scene: state.scene || null,
    tags: splitTags(state.tags),
    rating: state.rating,
    status: state.status,
    title: state.title.trim(),
    notes: state.notes.trim(),
    recipe: state.recipe.trim(),
    ...(state.card === null ? {} : { card: state.card.trim() }),
    inputs: assetIds(state.inputs, state.inputIds),
    outputs: assetIds(state.outputs, state.outputIds),
    source: state.source,
  }
}


// --- the classification editor -------------------------------------------------

interface Named {
  /** Stable while editing; a row just added has no id yet. */
  key: string
  id: string | null
  label: string
}
export interface TaxonomyForm {
  entries: { id: string; label: string; scenes: Named[] }[]
  industries: (Named & { shop_types: (Named & { keywords: string })[] })[]
}

export function fromTaxonomy(taxonomy: Taxonomy): TaxonomyForm {
  return {
    entries: taxonomy.entries.map((entry) => ({ id: entry.id, label: entry.label, scenes: (entry.scenes ?? []).map((scene) => ({ key: scene.id, id: scene.id, label: scene.label })) })),
    industries: taxonomy.industries.map((industry) => ({
      key: industry.id,
      id: industry.id,
      label: industry.label,
      shop_types: industry.shop_types.map((shop) => ({ key: shop.id, id: shop.id, label: shop.label, keywords: shop.keywords.join("、") })),
    })),
  }
}

function clash(items: { label: string }[]): boolean {
  const labels = items.map((item) => item.label.trim())
  return new Set(labels).size !== labels.length
}

/** Why this edit cannot be saved yet, as a key under `taxonomy.problem`; null when it can. */
export function taxonomyProblem(form: TaxonomyForm): "empty" | "duplicate" | null {
  const groups = [...form.entries.map((entry) => entry.scenes), form.industries, ...form.industries.map((industry) => industry.shop_types)]
  if (groups.some((group) => group.some((item) => !item.label.trim()))) return "empty"
  return groups.some(clash) ? "duplicate" : null
}

function splitKeywords(text: string): string[] {
  return Array.from(new Set(text.split(/[,，、\n]/).map((word) => word.trim().slice(0, 20)).filter(Boolean))).slice(0, 16)
}

const named = (item: Named) => ({ ...(item.id ? { id: item.id } : {}), label: item.label.trim() })

export function toTaxonomyWrite(form: TaxonomyForm): TaxonomyWrite {
  return {
    scenes: Object.fromEntries(form.entries.map((entry) => [entry.id, entry.scenes.map(named)])),
    industries: form.industries.map((industry) => ({
      ...named(industry),
      shop_types: industry.shop_types.map((shop) => ({ ...named(shop), keywords: splitKeywords(shop.keywords) })),
    })),
  }
}
