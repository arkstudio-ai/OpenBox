/** Human-facing copy never replaces the Skill's stable name or references. */
export interface SkillDisplay {
  display_name?: Record<string, string>
  display_description?: Record<string, string>
  package_display_name?: Record<string, string>
  package_display_description?: Record<string, string>
}

type SkillLabel = SkillDisplay & { name: string; title?: string; description?: string }

export function skillPackageDisplay<T extends SkillDisplay>(skill: T): T {
  return {
    ...skill,
    display_name: { ...skill.display_name, ...skill.package_display_name },
    display_description: { ...skill.display_description, ...skill.package_display_description },
  }
}

function localized(values: Record<string, string> | undefined, language: string, fallback: string): string {
  const locale = language.startsWith("zh") ? "zh-CN" : "en-US"
  return values?.[locale]?.trim() || values?.[locale === "zh-CN" ? "en-US" : "zh-CN"]?.trim() || fallback
}

export function skillDisplayName(skill: SkillLabel, language: string): string {
  return localized(skill.display_name, language, skill.title || skill.name)
}

export function skillDisplayDescription(skill: SkillLabel, language: string): string {
  return localized(skill.display_description, language, skill.description || "")
}

export function skillMatches(skill: SkillLabel, query: string): boolean {
  return [
    skill.name,
    skill.title,
    skill.description,
    ...Object.values(skill.display_name ?? {}),
    ...Object.values(skill.display_description ?? {}),
    ...Object.values(skill.package_display_name ?? {}),
    ...Object.values(skill.package_display_description ?? {}),
  ].some((text) => text?.toLowerCase().includes(query.trim().toLowerCase()))
}

export function cleanSkillDisplay(value: SkillDisplay): SkillDisplay {
  const result: SkillDisplay = {}
  for (const key of ["display_name", "display_description"] as const) {
    const entries = Object.entries(value[key] ?? {}).filter(([, text]) => text.trim())
    if (entries.length)
      result[key] = Object.fromEntries(entries.map(([locale, text]) => [locale, text.trim()]))
  }
  return result
}
