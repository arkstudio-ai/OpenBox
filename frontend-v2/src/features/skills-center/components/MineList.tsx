// What this account has installed: skills, then the MCP servers behind them.
import { useTranslation } from "react-i18next"
import { Plug, Trash2, Unplug } from "lucide-react"
import { Badge, EntryRow, IconButton } from "./EntryRow"
import type { InstalledSkill, McpServer } from "@/features/skills-center/types"

export interface MineActions {
  uninstallSkill: (dir: string) => void
  connect: (name: string) => void
  disconnect: (name: string) => void
  removeMcp: (name: string) => void
  busy: boolean
}

export function MineList({
  skills,
  servers,
  connectedNames,
  showSkills,
  showMcp,
  actions,
  onBrowseStore,
}: {
  skills: InstalledSkill[]
  servers: McpServer[]
  connectedNames: Set<string>
  showSkills: boolean
  showMcp: boolean
  actions: MineActions
  onBrowseStore: () => void
}) {
  const { t } = useTranslation("skills")

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
            {t("section.skills", { count: skills.length })}
          </h2>
          <div className="flex flex-col gap-1.5">
            {skills.map((s) => {
              // A declared dependency that is not connected is the difference
              // between "installed" and "will actually work".
              const missing = (s.requires_mcp ?? []).filter((m) => !connectedNames.has(m))
              return (
                <EntryRow
                  key={s.name}
                  icon={s.icon}
                  name={s.name}
                  description={s.description}
                  warning={
                    missing.length
                      ? t("mine.missingDependency", { names: missing.join(", ") })
                      : undefined
                  }
                  badges={s.source === "container" ? null : <Badge>{t("badge.host")}</Badge>}
                  actions={
                    s.source === "container" ? (
                      <IconButton
                        danger
                        title={t("action.uninstall")}
                        disabled={actions.busy}
                        onClick={() => actions.uninstallSkill(s.install_dir || s.name)}
                      >
                        <Trash2 size={14} />
                      </IconButton>
                    ) : null
                  }
                />
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
