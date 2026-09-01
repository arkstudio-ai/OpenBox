// What this account has installed: skills, then the MCP servers behind them.
import { useTranslation } from "react-i18next"
import { Plug, Trash2, Unplug } from "lucide-react"
import { Badge, EntryRow, IconButton } from "./EntryRow"
import { groupSkills } from "@/features/skills-center/lib/group-skills"
import type { InstalledSkill, McpServer } from "@/features/skills-center/types"
import { projectScopedDisplayPath } from "@/shared/lib/project-path"
import { SkillGroupsSection, type SkillGroupActions } from "./SkillGroupsSection"

export interface MineActions extends SkillGroupActions {
  connect: (name: string) => void
  disconnect: (name: string) => void
  removeMcp: (name: string) => void
}

export function mcpServerDescription(server: McpServer): string {
  if (server.url) return server.url
  return [server.command, ...(server.args ?? [])]
    .filter((value): value is string => Boolean(value))
    .map(projectScopedDisplayPath)
    .join(" ")
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
  const groups = groupSkills(skills)
  const personal = groups.filter((group) => group.category === "personal")
  const installed = groups.filter((group) => group.category !== "personal")

  if (skills.length === 0 && servers.length === 0) {
    return (
      <div className="border-hair flex flex-col items-center justify-center rounded-xl border border-dashed py-14 text-center">
        <p className="text-ink text-sm">{t("mine.emptyTitle")}</p>
        <p className="text-n600 mt-1 text-xs">{t("mine.emptyHint")}</p>
        <button
          type="button"
          onClick={onBrowseStore}
          className="bg-ink text-bg mt-3 rounded-full px-3.5 py-1.5 text-sm hover:opacity-90"
        >
          {t("mine.browseStore")}
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      {showSkills && personal.length > 0 ? (
        <SkillGroupsSection
          title={t("section.personal", { count: personal.length })}
          groups={personal}
          unmetFor={unmetFor}
          actions={actions}
        />
      ) : null}

      {showSkills && installed.length > 0 ? (
        <SkillGroupsSection
          title={t("section.skills", { count: installed.length })}
          groups={installed}
          unmetFor={unmetFor}
          actions={actions}
        />
      ) : null}

      {showMcp && servers.length > 0 && (
        <section>
          <h2 className="text-n600 mb-2 text-xs font-medium">
            {t("section.mcp", { count: servers.length })}
          </h2>
          <div className="flex flex-col gap-1.5">
            {servers.map((s) => (
              <EntryRow
                key={s.name}
                name={s.name}
                description={mcpServerDescription(s)}
                warning={s.status === "error" && s.error ? s.error : undefined}
                badges={
                  <>
                    <Badge tone={s.status === "connected" ? "ok" : s.status === "error" ? "warn" : "muted"}>
                      {t(`status.${s.status}`)}
                    </Badge>
                    {s.status === "connected" && <Badge>{t("badge.tools", { count: s.tools.length })}</Badge>}
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
