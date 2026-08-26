// Grouping installed skills back into the things that were actually installed.
//
// A skill pack is one install that unpacks into many skills: cloning
// anthropic/skills lands 19 SKILL.md files under one directory, and the scan
// reports 19 entries that all carry the same install_dir. Listing them flat
// makes "我的" read as 23 installs when the person performed four, and — worse
// — the uninstall on any one row is addressed by install_dir, so removing
// "docx" would take the other 18 with it without saying so.
import type { InstalledSkill } from "@/features/skills-center/types"

export interface SkillGroup {
  /** The directory the install produced — what uninstall actually removes. */
  id: string
  /** Shown as the row title. */
  name: string
  members: InstalledSkill[]
  /** A pack is anything one install left behind as more than one skill. */
  isPack: boolean
  /** Only container installs can be removed from here. */
  removable: boolean
  /** Where it came from — decides which badge the row wears. */
  origin: "container" | "builtin" | "host"
  icon?: string
  description?: string
}

export function groupSkills(skills: InstalledSkill[]): SkillGroup[] {
  const byDir = new Map<string, InstalledSkill[]>()
  for (const skill of skills) {
    // Host skills have no install_dir; they are their own group and are not
    // removable anyway.
    const key = skill.install_dir || skill.name
    const bucket = byDir.get(key)
    if (bucket) bucket.push(skill)
    else byDir.set(key, [skill])
  }

  const groups: SkillGroup[] = []
  for (const [id, members] of byDir) {
    const isPack = members.length > 1
    const first = members[0]
    groups.push({
      id,
      // A pack is named by its directory, since no single member's name
      // describes the whole thing.
      name: isPack ? id : first.name,
      members,
      isPack,
      removable: members.some((m) => m.source === "container"),
      origin: members.some((m) => m.source === "container")
        ? "container"
        : members.some((m) => m.source === "builtin")
          ? "builtin"
          : "host",
      icon: isPack ? undefined : first.icon,
      description: isPack ? undefined : first.description,
    })
  }

  // Packs first, then alphabetical: the biggest thing installed is the one
  // someone is most likely looking for.
  return groups.sort((a, b) => {
    if (a.isPack !== b.isPack) return a.isPack ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}
