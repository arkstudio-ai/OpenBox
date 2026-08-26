// 技能商店 — the catalogue, with what it depends on stated up front.
import { useTranslation } from "react-i18next"
import { ExternalLink } from "lucide-react"
import { Badge, EntryRow } from "./EntryRow"
import type { CatalogMcp, CatalogSkill } from "@/features/skills-center/types"

function Homepage({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={label}
      aria-label={label}
      className="flex size-7 items-center justify-center rounded-lg text-n600 hover:bg-card hover:text-ink"
    >
      <ExternalLink size={14} />
    </a>
  )
}

function InstallButton({
  installed,
  onInstall,
  installLabel,
  installedLabel,
}: {
  installed: boolean
  onInstall: () => void
  installLabel: string
  installedLabel: string
}) {
  return (
    <button
      type="button"
      disabled={installed}
      onClick={onInstall}
      className="rounded-full bg-ink px-3 py-1 text-xs text-bg hover:opacity-90 disabled:bg-n200 disabled:text-n700"
    >
      {installed ? installedLabel : installLabel}
    </button>
  )
}

export function StoreList({
  skills,
  mcp,
  showSkills,
  showMcp,
  onInstallSkill,
  onInstallMcp,
}: {
  skills: CatalogSkill[]
  mcp: CatalogMcp[]
  showSkills: boolean
  showMcp: boolean
  onInstallSkill: (entry: CatalogSkill) => void
  onInstallMcp: (entry: CatalogMcp) => void
}) {
  const { t } = useTranslation("skills")

  if (skills.length === 0 && mcp.length === 0) {
    return <p className="py-12 text-center text-sm text-n600">{t("store.noMatch")}</p>
  }

  return (
    <div className="flex flex-col gap-5">
      {showSkills && skills.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs font-medium text-n600">{t("section.storeSkills")}</h2>
          <div className="flex flex-col gap-1.5">
            {skills.map((s) => (
              <EntryRow
                key={s.id}
                icon={s.icon}
                name={s.title}
                description={s.description}
                badges={
                  <>
                    {s.publisher && <Badge>{s.publisher}</Badge>}
                    {/* Stated on the card, not just in the dialog: whether a
                        skill drags a server along changes whether someone
                        wants it at all. */}
                    {s.requires_mcp.length > 0 && (
                      <Badge tone="warn">
                        {t("badge.needsMcp", { names: s.requires_mcp.join(", ") })}
                      </Badge>
                    )}
                  </>
                }
                actions={
                  <>
                    {s.homepage && <Homepage href={s.homepage} label={t("action.homepage")} />}
                    <InstallButton
                      installed={s.installed}
                      onInstall={() => onInstallSkill(s)}
                      installLabel={t("action.install")}
                      installedLabel={t("action.installed")}
                    />
                  </>
                }
              />
            ))}
          </div>
        </section>
      )}

      {showMcp && mcp.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs font-medium text-n600">{t("section.storeMcp")}</h2>
          <div className="flex flex-col gap-1.5">
            {mcp.map((s) => (
              <EntryRow
                key={s.id}
                icon={s.icon}
                name={s.title}
                description={s.description}
                badges={
                  <>
                    {s.publisher && <Badge>{s.publisher}</Badge>}
                    <Badge>{t(`upload.transport.${s.config.type}`)}</Badge>
                    {s.required_env?.length ? (
                      <Badge tone="warn">{t("badge.needsKey")}</Badge>
                    ) : null}
                  </>
                }
                actions={
                  <>
                    {s.homepage && <Homepage href={s.homepage} label={t("action.homepage")} />}
                    <InstallButton
                      installed={s.installed}
                      onInstall={() => onInstallMcp(s)}
                      installLabel={t("action.install")}
                      installedLabel={t("action.installed")}
                    />
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
