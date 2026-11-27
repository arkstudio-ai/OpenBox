import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { Pagination } from "@/shared/ui/Pagination"

type Lookup = "phone" | "email"
interface Account {
  id: string
  username: string
  email: string | null
  phone: string | null
  default_workspace_id: string | null
}

const limit = 20

export default function AdminAccountsRoute() {
  const { t } = useTranslation("admin")
  const userId = useAuthStore((state) => state.user?.id)
  const [kind, setKind] = useState<Lookup>("phone")
  const [input, setInput] = useState("")
  const [query, setQuery] = useState<{ kind: Lookup; q: string; offset: number } | null>(null)
  const result = useQuery({
    queryKey: ["admin-account-search", userId, query],
    queryFn: () => http.post<{ items: Account[]; total: number }>("/api/admin/users/search", { ...query, limit }),
    enabled: query !== null,
    retry: false,
    gcTime: 0,
  })
  const columns: DataTableColumn<Account>[] = [
    { key: "username", header: t("accounts.username"), render: (row) => (
      <div><strong>{row.username}</strong><div className="text-n600">{row.id}</div></div>
    ) },
    { key: "phone", header: t("accounts.phone"), render: (row) => row.phone ?? "—" },
    { key: "email", header: t("accounts.email"), render: (row) => row.email ?? "—" },
    { key: "workspace", header: t("accounts.workspace"), render: (row) => row.default_workspace_id ? (
      <Link to={paths.adminWorkspace(row.default_workspace_id)} className="text-ink underline">
        {row.default_workspace_id}
      </Link>
    ) : "—" },
  ]
  const total = result.data?.total ?? 0
  const field = "min-h-9 rounded-lg border border-hair bg-bg px-3 text-sm text-ink"

  return (
    <div className="space-y-4">
      <form className="bg-card border-hair flex flex-wrap items-end gap-3 rounded-xl border p-4"
        onSubmit={(event) => {
          event.preventDefault()
          const q = input.trim()
          if (!q) return
          if (query?.q === q && query.kind === kind && query.offset === 0) void result.refetch()
          else setQuery({ kind, q, offset: 0 })
        }}>
        <label className="text-n700 flex flex-col gap-1 text-xs">
          {t("accounts.kind")}
          <select value={kind} className={field} onChange={(event) => {
            setKind(event.target.value as Lookup)
            setInput("")
            setQuery(null)
          }}>
            <option value="phone">{t("accounts.phone")}</option>
            <option value="email">{t("accounts.email")}</option>
          </select>
        </label>
        <label className="text-n700 flex min-w-40 flex-1 flex-col gap-1 text-xs">
          {t("accounts.keyword")}
          <input value={input} onChange={(event) => setInput(event.target.value)}
            inputMode={kind === "phone" ? "tel" : "email"} autoComplete="off" maxLength={255}
            className={field} placeholder={t(kind === "phone" ? "accounts.phoneHint" : "accounts.emailHint")} />
        </label>
        <button type="submit" disabled={!input.trim() || result.isFetching}
          className="bg-a700 text-bg min-h-9 rounded-full px-4 text-sm disabled:opacity-50">
          {t("accounts.search")}
        </button>
        <button type="button" className="border-hair min-h-9 rounded-full border px-4 text-sm"
          onClick={() => { setInput(""); setKind("phone"); setQuery(null) }}>
          {t("accounts.reset")}
        </button>
      </form>
      <section className="bg-card border-hair rounded-xl border p-4">
        <DataTable columns={columns} rows={result.data?.items ?? []} rowKey={(row) => row.id}
          isLoading={query !== null && result.isPending} error={result.error} minWidth="min-w-[30rem]"
          emptyText={t(query ? "accounts.empty" : "accounts.idle")} errorText={t("accounts.failed")}
          loadingLabel={t("accounts.loading")} />
        {query && total > 0 && (
          <Pagination offset={query.offset} limit={limit} total={total}
            labels={{ previous: t("accounts.previous"), next: t("accounts.next"),
              range: t("accounts.range", { count: total }) }}
            onOffsetChange={(offset) => setQuery({ ...query, offset })} />
        )}
      </section>
    </div>
  )
}
