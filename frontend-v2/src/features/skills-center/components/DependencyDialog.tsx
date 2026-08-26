// Resolving a skill's unmet MCP dependencies for the person, rather than
// telling them to go and do it.
//
// A skill declares the servers its instructions call. When one is missing the
// skill still loads and then fails at its first tool call, so naming the gap
// without offering to close it just moves the work onto whoever reads the
// warning. This offers every unmet dependency pre-checked, and installs or
// reconnects them in one go.
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { EntryIcon } from "./EntryIcon"
import type { CatalogMcp } from "@/features/skills-center/types"

/** One dependency and what has to happen to it. */
export interface Dependency {
  name: string
  /** Present when the store knows how to install it. */
  catalog?: CatalogMcp
  /** Already configured on the sandbox, just not connected. */
  configured: boolean
}

export interface DependencyTarget {
  skillName: string
  deps: Dependency[]
}

export function DependencyDialog({
  target,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  target: DependencyTarget
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: (deps: Dependency[], env: Record<string, Record<string, string>>) => void
}) {
  const { t } = useTranslation("skills")

  // A dependency the store does not carry cannot be installed from here; it is
  // listed so the gap is visible, but it is not offered as an action.
  const actionable = useMemo(
    () => target.deps.filter((d) => d.configured || d.catalog),
    [target.deps],
  )
  const unknown = useMemo(
    () => target.deps.filter((d) => !d.configured && !d.catalog),
    [target.deps],
  )

  // Held as the cleared set so "everything checked" is the default with no
  // effect to install it.
  const [cleared, setCleared] = useState<string[]>([])
  const [envValues, setEnvValues] = useState<Record<string, Record<string, string>>>({})

  const selected = useMemo(
    () => actionable.filter((d) => !cleared.includes(d.name)),
    [actionable, cleared],
  )

  const envNeeded = useMemo(
    () =>
      selected
        .map((d) => d.catalog)
        .filter((c): c is CatalogMcp => Boolean(c?.required_env?.length)),
    [selected],
  )

  const missingRequired = envNeeded.some((server) =>
    (server.required_env ?? []).some((f) => !(envValues[server.id]?.[f.key] ?? "").trim()),
  )

  function toggle(name: string) {
    setCleared((prev) =>
      prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name],
    )
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("deps.title")}
    >
      <div className="flex max-h-[82vh] w-full max-w-[480px] flex-col overflow-hidden rounded-2xl border border-hair bg-card shadow-xl">
        <div className="px-5 pb-3 pt-5">
          <h2 className="text-base font-medium text-ink">{t("deps.title")}</h2>
          <p className="mt-1 text-xs leading-5 text-n600">
            {t("deps.subtitle", { skill: target.skillName })}
          </p>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5">
          <ul className="flex flex-col gap-1.5">
            {actionable.map((dep) => (
              <li key={dep.name}>
                <label className="flex cursor-pointer items-center gap-2.5 rounded-lg bg-hairsoft/40 px-2.5 py-2 hover:bg-hairsoft/70">
                  <input
                    type="checkbox"
                    checked={!cleared.includes(dep.name)}
                    onChange={() => toggle(dep.name)}
                    className="size-3.5 accent-accent"
                  />
                  <EntryIcon icon={dep.catalog?.icon} name={dep.catalog?.title ?? dep.name} size="sm" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink">
                      {dep.catalog?.title ?? dep.name}
                    </span>
                    <span className="block truncate text-xs text-n600">
                      {dep.configured ? t("deps.willConnect") : t("deps.willInstall")}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>

          {unknown.length > 0 && (
            <p className="mt-3 rounded-lg bg-hairsoft/50 px-3 py-2 text-xs leading-5 text-n600">
              {t("deps.unknown", { names: unknown.map((d) => d.name).join(", ") })}
            </p>
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
                    onChange={(e) =>
                      setEnvValues((prev) => ({
                        ...prev,
                        [server.id]: { ...(prev[server.id] ?? {}), [field.key]: e.target.value },
                      }))
                    }
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
            {t("deps.later")}
          </button>
          <button
            type="button"
            onClick={() => onConfirm(selected, envValues)}
            disabled={busy || missingRequired || selected.length === 0}
            className="rounded-full bg-ink px-3.5 py-1.5 text-sm text-bg hover:opacity-90 disabled:opacity-50"
          >
            {busy ? t("deps.working") : t("deps.confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}
