import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, ChevronRight, Download, Trash2, UploadCloud } from "lucide-react"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import type { InstalledSkill } from "@/features/skills-center/types"
import { Badge, EntryRow, IconButton } from "./EntryRow"

export interface SkillGroupActions {
  uninstallSkill: (dir: string, count: number) => void
  fixDependencies: (skill: InstalledSkill) => void
  publishSkill: (group: SkillGroup) => void
  downloadSkill: (dir: string) => void
  busy: boolean
}

interface Props {
  title: string
  groups: SkillGroup[]
  unmetFor: (skill: InstalledSkill) => { name: string }[]
  actions: SkillGroupActions
}

export function SkillGroupsSection({ title, groups, unmetFor, actions }: Props) {
  const { t } = useTranslation("skills")
  const [open, setOpen] = useState<string[]>([])

  return (
    <section>
      <h2 className="text-n600 mb-2 text-xs font-medium">{title}</h2>
      <div className="flex flex-col gap-1.5">
        {groups.map((group) => {
          const missing = [
            ...new Set(group.members.flatMap((member) => unmetFor(member).map((dep) => dep.name))),
          ]
          const expanded = open.includes(group.id)
          const personal = group.category === "personal"
          return (
            <div key={group.id}>
              <EntryRow
                icon={group.icon}
                name={group.name}
                description={
                  group.isPack ? group.members.map((member) => member.name).join(", ") : group.description
                }
                warning={
                  missing.length ? t("mine.missingDependency", { names: missing.join(", ") }) : undefined
                }
                onFixWarning={missing.length ? () => actions.fixDependencies(group.members[0]) : undefined}
                fixLabel={t("deps.fixNow")}
                fixDisabled={actions.busy}
                badges={
                  <>
                    {group.isPack ? (
                      <Badge>{t("badge.packCount", { count: group.members.length })}</Badge>
                    ) : null}
                    {personal ? (
                      <>
                        <Badge>{t("badge.personal")}</Badge>
                        <Badge tone={group.publicationStatus === "published" ? "ok" : "warn"}>
                          {t(
                            group.publicationStatus === "published" ? "badge.published" : "badge.unpublished",
                          )}
                        </Badge>
                      </>
                    ) : group.category === "store" ? (
                      <Badge>{t("badge.storeInstalled")}</Badge>
                    ) : group.origin !== "container" ? (
                      <Badge title={t(`badge.${group.origin}Hint`)}>{t(`badge.${group.origin}`)}</Badge>
                    ) : null}
                  </>
                }
                actions={
                  <>
                    {personal ? (
                      <>
                        <IconButton
                          title={t(
                            group.publicationStatus === "published"
                              ? "action.updatePublish"
                              : "action.publish",
                          )}
                          disabled={actions.busy}
                          onClick={() => actions.publishSkill(group)}
                        >
                          <UploadCloud size={14} />
                        </IconButton>
                        <IconButton
                          title={t("action.download")}
                          disabled={actions.busy}
                          onClick={() => actions.downloadSkill(group.id)}
                        >
                          <Download size={14} />
                        </IconButton>
                      </>
                    ) : null}
                    {group.isPack ? (
                      <IconButton
                        title={expanded ? t("action.collapse") : t("action.expand")}
                        onClick={() =>
                          setOpen((previous) =>
                            previous.includes(group.id)
                              ? previous.filter((id) => id !== group.id)
                              : [...previous, group.id],
                          )
                        }
                      >
                        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </IconButton>
                    ) : null}
                    {group.removable ? (
                      <IconButton
                        danger
                        title={t("action.uninstall")}
                        disabled={actions.busy}
                        onClick={() => actions.uninstallSkill(group.id, group.members.length)}
                      >
                        <Trash2 size={14} />
                      </IconButton>
                    ) : null}
                  </>
                }
              />
              {group.isPack && expanded ? (
                <ul className="border-hair mt-1 ml-6 flex flex-col gap-1 border-l pl-3">
                  {group.members.map((member) => (
                    <li key={member.name} className="flex items-baseline gap-2 py-0.5">
                      <span className="text-ink text-xs">{member.name}</span>
                      <span className="text-n600 min-w-0 flex-1 truncate text-xs">{member.description}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          )
        })}
      </div>
    </section>
  )
}
