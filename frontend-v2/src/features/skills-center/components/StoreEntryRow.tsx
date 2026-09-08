// One shelf entry, whichever kind it is.
//
// Skills and MCP servers share a row because a shelf mixes them: sections are
// cut by who published the thing, not by what it technically is, so the kind
// has to be readable on the row itself rather than inferred from the heading.
import { useTranslation } from "react-i18next"
import { ExternalLink } from "lucide-react"
import { Badge, EntryRow } from "./EntryRow"
import type { StoreEntry } from "@/features/skills-center/lib/store-sections"

function Homepage({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={label}
      aria-label={label}
      className="text-n600 hover:bg-card hover:text-ink flex size-7 items-center justify-center rounded-lg"
    >
      <ExternalLink size={14} />
    </a>
  )
}

export function StoreEntryRow({ entry, onInstall }: { entry: StoreEntry; onInstall: () => void }) {
  const { t } = useTranslation("skills")
  const installs = entry.installs_count ?? 0

  return (
    <EntryRow
      icon={entry.icon}
      name={entry.title}
      description={entry.description}
      badges={
        <>
          {/* Pinned by an operator, so the row says why it is at the top
              instead of looking like an accident of sorting. */}
          {entry.featured ? <Badge tone="warn">{t("badge.featured")}</Badge> : null}
          <Badge>{t(entry.kind === "mcp" ? "badge.kindMcp" : "badge.kindSkill")}</Badge>
          {entry.publisher && <Badge>{entry.publisher}</Badge>}
          {entry.kind === "mcp" ? (
            <>
              <Badge>{t(`upload.transport.${entry.config.type}`)}</Badge>
              {entry.required_env?.length ? <Badge tone="warn">{t("badge.needsKey")}</Badge> : null}
            </>
          ) : (
            /* Stated on the card, not just in the dialog: whether a skill
               drags a server along changes whether someone wants it at all. */
            entry.requires_mcp.length > 0 && (
              <Badge tone="warn">{t("badge.needsMcp", { names: entry.requires_mcp.join(", ") })}</Badge>
            )
          )}
          {/* Zero is not evidence of anything — a fresh entry and an ignored
              one look identical — so the count appears once it means something. */}
          {installs > 0 ? <Badge>{t("badge.installs", { count: installs })}</Badge> : null}
        </>
      }
      actions={
        <>
          {entry.homepage && <Homepage href={entry.homepage} label={t("action.homepage")} />}
          <button
            type="button"
            disabled={entry.installed}
            onClick={onInstall}
            className="bg-ink text-bg disabled:bg-n200 disabled:text-n700 rounded-full px-3 py-1 text-xs hover:opacity-90"
          >
            {entry.installed ? t("action.installed") : t("action.install")}
          </button>
        </>
      }
    />
  )
}
