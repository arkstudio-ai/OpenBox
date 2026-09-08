// A titled run of installed groups. The row itself lives next door — it grew
// a listing chip, a reason line and a withdraw action, and a section that also
// knows how to draw all of that stops being a section.
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import type { InstalledSkill } from "@/features/skills-center/types"
import { SkillGroupRow } from "./SkillGroupRow"

export type { SkillGroupActions } from "./SkillGroupRow"
import type { SkillGroupActions } from "./SkillGroupRow"

interface Props {
  title: string
  groups: SkillGroup[]
  unmetFor: (skill: InstalledSkill) => { name: string }[]
  actions: SkillGroupActions
}

export function SkillGroupsSection({ title, groups, unmetFor, actions }: Props) {
  return (
    <section>
      <h2 className="text-n600 mb-2 text-xs font-medium">{title}</h2>
      <div className="flex flex-col gap-1.5">
        {groups.map((group) => (
          <SkillGroupRow key={group.id} group={group} unmetFor={unmetFor} actions={actions} />
        ))}
      </div>
    </section>
  )
}
