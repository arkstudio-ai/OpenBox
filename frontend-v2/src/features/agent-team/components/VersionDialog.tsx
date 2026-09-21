import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogTitle, DialogActions } from "@/shared/ui/Dialog"
import { Spinner } from "@/shared/ui/Spinner"
import { useVersions } from "../api/teams"
import type { AgentSpec, Definition, TeamSpec } from "../types"
import { versionChanges } from "../lib/version-diff"
import { BUTTON, INPUT } from "./FormFields"

type Spec = AgentSpec | TeamSpec
const SIDES = ["before", "after"] as const

export function VersionDialog({
  kind,
  definition,
  onClose,
}: {
  kind: "agent" | "team"
  definition: Definition<Spec>
  onClose: () => void
}) {
  const { t } = useTranslation("teams")
  const versions = useVersions<Spec>(kind, definition.id)
  const [fromId, setFromId] = useState("")
  const [toId, setToId] = useState("")
  const rows = (versions.data?.pages.flatMap((page) => page.items) ?? []).sort(
    (a, b) => b.version - a.version,
  )
  const before = rows.find((row) => row.id === fromId) ?? rows[1] ?? rows[0]
  const after = rows.find((row) => row.id === toId) ?? rows[0]
  const changes = before && after ? versionChanges(before.spec, after.spec) : []
  const selectors = [
    { label: "versionBefore", id: before?.id, change: setFromId },
    { label: "versionAfter", id: after?.id, change: setToId },
  ] as const
  const display = (value: unknown) =>
    value === undefined
      ? t("versionUnset")
      : typeof value === "string"
        ? value
        : JSON.stringify(value, null, 2)

  return (
    <Dialog open onClose={onClose} wide label={t("versions")}>
      <DialogTitle>{t("versions")}</DialogTitle>
      {versions.isLoading && <Spinner className="size-5" />}
      {versions.error && <p className="text-danger text-sm">{versions.error.message}</p>}
      {rows.length > 1 && before && after && (
        <section aria-label={t("compareVersions")} className="border-hair space-y-3 rounded-xl border p-3">
          <h3 className="text-sm font-medium">{t("compareVersions")}</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {selectors.map(({ label, id, change }) => (
              <label key={label} className="text-n600 text-xs">
                {t(label)}
                <select
                  className={`${INPUT} mt-1.5`}
                  value={id}
                  onChange={(event) => change(event.target.value)}
                >
                  {rows.map((row) => (
                    <option key={row.id} value={row.id}>
                      {t("version", { version: row.version })}
                      {row.id === definition.current_version_id ? ` · ${t("published")}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <p className="text-n600 text-xs" role="status">
            {t(changes.length ? "proposedChanges" : "versionNoChanges", { count: changes.length })}
          </p>
          <dl className="space-y-3">
            {changes.map((change) => (
              <div key={change.key}>
                <dt className="mb-1.5 text-xs font-medium">
                  {t(`specFields.${change.key}`, { defaultValue: change.key })}
                </dt>
                <dd className="grid gap-2 sm:grid-cols-2">
                  {SIDES.map((side) => (
                    <div key={side} className="bg-hairsoft min-w-0 rounded-lg p-2">
                      <p className="text-n500 mb-1 text-xs">
                        {t(side === "before" ? "versionBefore" : "versionAfter")}
                      </p>
                      <pre className="scr max-h-56 overflow-auto text-xs [overflow-wrap:anywhere] whitespace-pre-wrap">
                        {display(change[side])}
                      </pre>
                    </div>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {rows.length === 1 && <p className="text-n600 text-sm">{t("versionOnlyOne")}</p>}
      {rows.map((version) => (
        <details key={version.id} className="border-hair border-b py-3">
          <summary className="cursor-pointer text-sm">
            {t("version", { version: version.version })} · {version.spec.name}{" "}
            {version.id === definition.current_version_id && (
              <span className="text-s600">{t("published")}</span>
            )}
          </summary>
          <p className="text-n600 my-2 text-xs">
            {version.created_at ? new Date(version.created_at).toLocaleString() : ""}
          </p>
          <pre className="scr bg-hairsoft max-h-80 overflow-auto rounded-lg p-3 text-xs">
            {JSON.stringify(version.spec, null, 2)}
          </pre>
        </details>
      ))}
      {versions.hasNextPage && (
        <button type="button" onClick={() => void versions.fetchNextPage()} className={BUTTON}>
          {t("loadMore")}
        </button>
      )}
      <DialogActions>
        <button type="button" onClick={onClose} className={BUTTON}>
          {t("close")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
