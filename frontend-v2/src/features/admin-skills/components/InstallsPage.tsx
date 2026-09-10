// 技能管理 › 用户安装 — the install ledger, by skill or by person.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { X } from "lucide-react"
import { Pagination } from "@/shared/ui/Pagination"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { useInstalls } from "@/features/admin-skills/api/admin-skills"
import { InstallsTable } from "./InstallsTable"
import { SearchBox } from "./SearchBox"
import { DesktopInstalls } from "./DesktopInstalls"

const LIMIT = 20
// `catalog` is prefilled when an operator clicks an install count in the store.
const DEFAULTS = { q: "", catalog: "", offset: "0" }
const MODES = ["live", "history"] as const

export function InstallsPage() {
  const { t } = useTranslation("admin-skills")
  const [params] = useSearchParams()
  const [mode, setMode] = useState(params.has("catalog") ? "history" : "live")
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {MODES.map((tab) => (
          <button
            type="button"
            key={tab}
            aria-pressed={mode === tab}
            className={`rounded-full px-4 py-2 text-sm ${mode === tab ? "bg-hairsoft font-medium" : "text-n600"}`}
            onClick={() => setMode(tab)}
          >
            {t(`desktop.${tab}`)}
          </button>
        ))}
      </div>
      {mode === "live" ? <DesktopInstalls /> : <InstallHistory />}
    </div>
  )
}

function InstallHistory() {
  const { t } = useTranslation("admin-skills")
  const [values, setValues] = useUrlState(DEFAULTS)
  const offset = Number(values.offset) || 0
  const query = useInstalls({
    q: values.q,
    catalog_id: values.catalog,
    offset,
    limit: LIMIT,
  })
  const total = query.data?.total ?? 0

  return (
    <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <SearchBox
          value={values.q}
          onChange={(q) => setValues({ q })}
          label={t("installs.searchLabel")}
          placeholder={t("installs.search")}
        />
        {/* The chip states the filter; the × next to it is the only control,
            so its name is the action rather than the catalog id. */}
        {values.catalog && (
          <span className="border-hair text-n800 flex flex-none items-center gap-1.5 rounded-full border py-1 ps-3 pe-1.5 text-xs">
            <span className="max-w-56 truncate font-mono">
              {t("installs.filtered", { catalog: values.catalog })}
            </span>
            <button
              type="button"
              onClick={() => setValues({ catalog: "" })}
              aria-label={t("installs.clearFilter")}
              className="hover:bg-hairsoft rounded-full p-1"
            >
              <X className="size-3" aria-hidden />
            </button>
          </span>
        )}
      </div>

      <InstallsTable rows={query.data?.items ?? []} isLoading={query.isPending} error={query.error} />

      {total > 0 && (
        <Pagination
          offset={offset}
          limit={LIMIT}
          total={total}
          onOffsetChange={(next) => setValues({ offset: String(next) })}
          labels={{
            previous: t("list.previous"),
            next: t("list.next"),
            nav: t("installs.nav"),
            range: t("list.range", { from: offset + 1, to: Math.min(offset + LIMIT, total), total }),
          }}
        />
      )}

      {/* The ledger records installs; it is not a live scan of anyone's sandbox. */}
      <p className="text-n600 border-hair text-2xs border-t pt-3">{t("installs.footnote")}</p>
    </section>
  )
}
