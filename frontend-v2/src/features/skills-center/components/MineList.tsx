// What this account has installed: skills, then the MCP servers behind them.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, ChevronRight, Plug, Trash2, Unplug } from "lucide-react"
import { Badge, EntryRow, IconButton } from "./EntryRow"
import { groupSkills } from "@/features/skills-center/lib/group-skills"
import type { InstalledSkill, McpServer } from "@/features/skills-center/types"

export interface MineActions {
  /** `count` is how many skills the removal actually takes with it. */
  uninstallSkill: (dir: string, count: number) => void
  /** Offer to install/connect what a skill still needs. */
  fixDependencies: (skill: InstalledSkill) => void
  connect: (name: string) => void
  disconnect: (name: string) => void
  removeMcp: (name: string) => void
  busy: boolean
}

export function MineList({
  skills,
  servers,
  unmetFor,
  showSkills,
  showMcp,
  actions,
  onBrowseStore,
}: {
  skills: InstalledSkill[]
  servers: McpServer[]
  /** Declared servers that are not usable yet, per skill. */
  unmetFor: (skill: InstalledSkill) => { name: string }[]
  showSkills: boolean
  showMcp: boolean
  actions: MineActions
  onBrowseStore: () => void
}) {
  const { t } = useTranslation("skills")
  const [open, setOpen] = useState<string[]>([])
  const groups = groupSkills(skills)

  if (skills.length === 0 && servers.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-hair py-14 text-center">
        <p className="text-sm text-ink">{t("mine.emptyTitle")}</p>
        <p className="mt-1 text-xs text-n600">{t("mine.emptyHint")}</p>
        <button
          type="button"
          onClick={onBrowseStore}
          className="mt-3 rounded-full bg-ink px-3.5 py-1.5 text-sm text-bg hover:opacity-90"
        >
          {t("mine.browseStore")}
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      {showSkills && skills.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs font-medium text-n600">
            {t("section.skills", { count: groups.length })}
          </h2>
          <div className="flex flex-col gap-1.5">
            {groups.map((group) => {
              // Every member's gaps roll up: a pack is only usable when all of
              // its skills are.
              const missing = [
                ...new Set(group.members.flatMap((m) => unmetFor(m).map((d) => d.name))),
              ]
              const expanded = open.includes(group.id)
              return (
                <div key={group.id}>
                  <EntryRow
                    icon={group.icon}
                    name={group.name}
                    description={
                      group.isPack
                        ? group.members.map((m) => m.name).join(", ")
                        : group.description
                    }
                    warning={
                      missing.length
                        ? t("mine.missingDependency", { names: missing.join(", ") })
                        : undefined
                    }
                    onFixWarning={
                      missing.length ? () => actions.fixDependencies(group.members[0]) : undefined
                    }
                    fixLabel={t("deps.fixNow")}
                    fixDisabled={actions.busy}
                    badges={
                      <>
                        {group.isPack && (
                          <Badge>{t("badge.packCount", { count: group.members.length })}</Badge>
                        )}
                        {!group.removable && <Badge>{t("badge.host")}</Badge>}
                      </>
                    }
                    actions={
                      <>
                        {group.isPack && (
                          <IconButton
                            title={expanded ? t("action.collapse") : t("action.expand")}
                            onClick={() =>
                              setOpen((prev) =>
                                prev.includes(group.id)
                                  ? prev.filter((x) => x !== group.id)
                                  : [...prev, group.id],
                              )
                            }
                          >
                            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          </IconButton>
                        )}
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
                  {group.isPack && expanded && (
                    <ul className="mt-1 ml-6 flex flex-col gap-1 border-l border-hair pl-3">
                      {group.members.map((m) => (
                        <li key={m.name} className="flex items-baseline gap-2 py-0.5">
                          <span className="text-xs text-ink">{m.name}</span>
                          <span className="min-w-0 flex-1 truncate text-xs text-n600">
                            {m.description}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {showMcp && servers.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs font-medium text-n600">
            {t("section.mcp", { count: servers.length })}
          </h2>
          <div className="flex flex-col gap-1.5">
            {servers.map((s) => (
              <EntryRow
                key={s.name}
                name={s.name}
                description={s.url || [s.command, ...(s.args ?? [])].filter(Boolean).join(" ")}
                warning={s.status === "error" && s.error ? s.error : undefined}
                badges={
                  <>
                    <Badge
                      tone={
                        s.status === "connected" ? "ok" : s.status === "error" ? "warn" : "muted"
                      }
                    >
                      {t(`status.${s.status}`)}
                    </Badge>
                    {s.status === "connected" && (
                      <Badge>{t("badge.tools", { count: s.tools.length })}</Badge>
                    )}
                  </>
                }
                actions={
                  <>
                    {s.status === "connected" ? (
                      <IconButton
                        title={t("action.disconnect")}
                        disabled={actions.busy}
                        onClick={() => actions.disconnect(s.name)}
                      >
                        <Unplug size={14} />
                      </IconButton>
                    ) : (
                      <IconButton
                        title={t("action.connect")}
                        disabled={actions.busy}
                        onClick={() => actions.connect(s.name)}
                      >
                        <Plug size={14} />
                      </IconButton>
                    )}
                    <IconButton
                      danger
                      title={t("action.remove")}
                      disabled={actions.busy}
                      onClick={() => actions.removeMcp(s.name)}
                    >
                      <Trash2 size={14} />
                    </IconButton>
                  </>
                }
              />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
