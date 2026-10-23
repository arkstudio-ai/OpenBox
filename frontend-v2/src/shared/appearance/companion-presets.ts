// Stable IDs for the supplied IP portraits. Never reuse an ID for another character.
export const COMPANION_PRESETS = [
  "ip-01",
  "ip-02",
  "ip-03",
  "ip-04",
  "ip-05",
  "ip-06",
  "ip-07",
  "ip-08",
  "ip-09",
  "ip-10",
  "ip-11",
  "ip-12",
  "ip-13",
  "ip-14",
  "ip-15",
  "ip-16",
  "ip-17",
  "ip-18",
  "ip-19",
  "ip-20",
  "ip-21",
  "ip-22",
  "ip-23",
  "ip-24",
  "ip-25",
  "ip-26",
  "ip-27",
  "ip-28",
  "ip-29",
  "ip-30",
  "ip-31",
  "ip-32",
  "ip-33",
  "ip-34",
] as const
export type BuiltinCompanionPreset = (typeof COMPANION_PRESETS)[number]
export type CompanionPreset = BuiltinCompanionPreset | "custom"
export const DEFAULT_COMPANION_PRESET: BuiltinCompanionPreset = "ip-01"

const LEGACY_PRESETS: Record<string, BuiltinCompanionPreset> = {
  orb: "ip-01",
  sprout: "ip-10",
  cat: "ip-12",
}

/** Also handles cached responses from a previous backend during an upgrade. */
export function resolveCompanionPreset(preset: string): CompanionPreset {
  if (preset === "custom" || (COMPANION_PRESETS as readonly string[]).includes(preset))
    return preset as CompanionPreset
  return Object.hasOwn(LEGACY_PRESETS, preset) ? LEGACY_PRESETS[preset] : DEFAULT_COMPANION_PRESET
}

const portraits = import.meta.glob<string>("./portraits/*.webp", {
  eager: true,
  query: "?url",
  import: "default",
})

/** Vite fingerprints the local assets; selecting a preset never needs an expiring OSS URL. */
export function companionPortrait(preset: string): string {
  const resolved = resolveCompanionPreset(preset)
  return portraits[`./portraits/${resolved === "custom" ? DEFAULT_COMPANION_PRESET : resolved}.webp`]
}
