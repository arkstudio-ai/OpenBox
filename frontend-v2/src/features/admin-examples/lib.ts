// Form state and the pure conversions behind the example sheet, kept out of
// the component file so fast refresh keeps working.
import type { AssetRef, ExampleWrite } from "./types"

export interface FormState {
  entry_id: string
  industry: string
  shop_type: string
  subtype: string
  title: string
  tags: string
  rating: number
  notes: string
  recipe: string
  inputs: AssetRef[]
  outputs: AssetRef[]
  inputIds: string
  outputIds: string
  source: ExampleWrite["source"]
  status: ExampleWrite["status"]
}

export const EMPTY: FormState = {
  entry_id: "video",
  industry: "food",
  shop_type: "other",
  subtype: "",
  title: "",
  tags: "",
  rating: 4,
  notes: "",
  recipe: "",
  inputs: [],
  outputs: [],
  inputIds: "",
  outputIds: "",
  source: {},
  status: "active",
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
    tags: splitTags(state.tags),
    rating: state.rating,
    status: state.status,
    title: state.title.trim(),
    notes: state.notes.trim(),
    recipe: state.recipe.trim(),
    inputs: assetIds(state.inputs, state.inputIds),
    outputs: assetIds(state.outputs, state.outputIds),
    source: state.source,
  }
}

