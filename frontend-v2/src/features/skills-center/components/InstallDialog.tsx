// Confirming a store install, and collecting what the entry needs first.
//
// Two things have to happen before an install is safe to fire: any MCP servers
// the skill depends on must be offered (a skill whose server is missing loads
// and then fails at its first tool call, which reads as a broken skill), and
// any credentials the server declares must be collected (a server installed
// without its key connects and fails on every call).
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { EntryIcon } from "./EntryIcon"
import type { CatalogMcp, CatalogSkill } from "@/features/skills-center/types"

export interface InstallTarget {
  kind: "skill" | "mcp"
  entry: CatalogSkill | CatalogMcp
}

export function InstallDialog({
  target,
  mcpCatalog,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  target: InstallTarget
  mcpCatalog: CatalogMcp[]
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: (withMcp: string[], env: Record<string, Record<string, string>>) => void
}) {
  const { t } = useTranslation("skills")
  const entry = target.entry

  // Dependencies still missing, resolved to their catalogue entries so the
  // dialog can show what it is about to install rather than bare ids.
  const missing = useMemo(() => {
    if (target.kind !== "skill") return []
    const ids = (entry as CatalogSkill).missing_mcp ?? []
    return ids
      .map((id) => mcpCatalog.find((m) => m.id === id))
      .filter((m): m is CatalogMcp => Boolean(m))
  }, [target.kind, entry, mcpCatalog])

  // Dependencies default to checked: leaving one off is the unusual choice, and
  // it is the choice that produces a skill that does not work. Held as the set
  // of *cleared* ids so the default needs no effect to install it — the dialog
  // is mounted per target, so there is nothing to reset between opens either.
  const [cleared, setCleared] = useState<string[]>([])
  const [envValues, setEnvValues] = useState<Record<string, Record<string, string>>>({})

  const selected = useMemo(
    () => missing.map((m) => m.id).filter((id) => !cleared.includes(id)),
    [missing, cleared],
  )

  // Every server about to be installed that wants credentials — the target
  // itself when installing an MCP entry, plus each selected dependency.
  const envNeeded = useMemo(() => {
    const list: CatalogMcp[] = []
    if (target.kind === "mcp" && (entry as CatalogMcp).required_env?.length) {
      list.push(entry as CatalogMcp)
    }
    for (const dep of missing) {
      if (selected.includes(dep.id) && dep.required_env?.length) list.push(dep)
    }
    return list
  }, [target.kind, entry, missing, selected])

  const missingRequired = envNeeded.some((server) =>
    (server.required_env ?? []).some((field) => !(envValues[server.id]?.[field.key] ?? "").trim()),
  )

  function toggle(id: string) {
    setCleared((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  function setEnv(serverId: string, key: string, value: string) {
    setEnvValues((prev) => ({ ...prev, [serverId]: { ...(prev[serverId] ?? {}), [key]: value } }))
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("install.title")}
    >
      <div className="flex max-h-[82vh] w-full max-w-[480px] flex-col overflow-hidden rounded-2xl border border-hair bg-card shadow-xl">
        <div className="flex items-start gap-3 px-5 pb-3 pt-5">
          <EntryIcon icon={entry.icon} name={entry.title} />
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-base font-medium text-ink">{entry.title}</h2>
            <p className="mt-0.5 text-xs leading-5 text-n600">{entry.description}</p>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5">
          {missing.length > 0 && (
            <section className="mt-1 rounded-xl border border-hair bg-hairsoft/50 p-3">
              <p className="text-xs font-medium text-ink">{t("install.dependsTitle")}</p>
              <p className="mt-0.5 text-xs leading-5 text-n600">{t("install.dependsHint")}</p>
              <ul className="mt-2.5 flex flex-col gap-1.5">
                {missing.map((dep) => (
                  <li key={dep.id}>
                    <label className="flex cursor-pointer items-center gap-2.5 rounded-lg px-1.5 py-1.5 hover:bg-hairsoft">
                      <input
                        type="checkbox"
                        checked={selected.includes(dep.id)}
                        onChange={() => toggle(dep.id)}
                        className="size-3.5 accent-accent"
                      />
                      <EntryIcon icon={dep.icon} name={dep.title} size="sm" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-ink">{dep.title}</span>
                        <span className="block truncate text-xs text-n600">{dep.description}</span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
              {selected.length < missing.length && (
                <p className="mt-2 text-xs leading-5 text-sage">
                  {t("install.dependsWarning")}
                </p>
              )}
            </section>
          )}

          {envNeeded.map((server) => (
            <section key={server.id} className="mt-3">
              <p className="text-xs font-medium text-ink">
                {t("install.credentialsFor", { name: server.title })}
              </p>
              {(server.required_env ?? []).map((field) => (
                <label key={field.key} className="mt-2 block">
                  <span className="text-xs text-n600">{field.label}</span>
                  <input
                    type={field.secret ? "password" : "text"}
                    value={envValues[server.id]?.[field.key] ?? ""}
                    onChange={(e) => setEnv(server.id, field.key, e.target.value)}
                    placeholder={field.key}
                    autoComplete="off"
                    className="mt-1 w-full rounded-lg border border-hair bg-canvas px-2.5 py-1.5 text-sm text-ink outline-none focus:border-accent"
                  />
                </label>
              ))}
            </section>
          ))}

          {error && (
            <p className="mt-3 rounded-lg bg-dangersoft px-3 py-2 text-xs leading-5 text-danger">
              {error}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 pb-5 pt-4">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-full px-3.5 py-1.5 text-sm text-n700 hover:bg-hairsoft disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={() => onConfirm(selected, envValues)}
            disabled={busy || missingRequired}
            className="rounded-full bg-ink px-3.5 py-1.5 text-sm text-bg hover:opacity-90 disabled:opacity-50"
          >
            {busy ? t("install.installing") : t("install.confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}
