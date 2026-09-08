// The three columns of 技能管理. The route reads `:tab` and falls back to the
// first entry, so the order here is also the default.
export const ADMIN_SKILL_TABS = ["store", "review", "installs"] as const

export type AdminSkillTab = (typeof ADMIN_SKILL_TABS)[number]

export function isAdminSkillTab(value: string | undefined): value is AdminSkillTab {
  return ADMIN_SKILL_TABS.some((tab) => tab === value)
}
