// 技能管理 › 商店上架 — every catalogue and community entry in one list, with
// the shelf controls next to each row.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { Pagination } from "@/shared/ui/Pagination"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { paths } from "@/shared/router/paths"
import {
  useSetFeatured,
  useSetListing,
  useSetOfficial,
  useStoreEntries,
} from "@/features/admin-skills/api/admin-skills"
import { ANY } from "@/features/admin-skills/lib/query"
import type { StoreEntry } from "@/features/admin-skills/types"
import { ListingDialog } from "./ListingDialog"
import { StoreFilters } from "./StoreFilters"
import { StoreTable } from "./StoreTable"

const LIMIT = 20
const DEFAULTS = { q: "", origin: ANY, kind: ANY, listing: ANY, offset: "0" }

/** Which confirmation is open, and for which row. */
interface Confirming {
  action: "list" | "delist"
  entry: StoreEntry
}

export function StorePage() {
  const { t } = useTranslation("admin-skills")
  const navigate = useNavigate()
  const [values, setValues] = useUrlState(DEFAULTS)
  const [confirming, setConfirming] = useState<Confirming | null>(null)

  const offset = Number(values.offset) || 0
  const query = useStoreEntries({
    q: values.q,
    origin: values.origin,
    kind: values.kind,
    listing: values.listing,
    offset,
    limit: LIMIT,
  })
  const setListing = useSetListing()
  const setFeatured = useSetFeatured()
  const setOfficial = useSetOfficial()

  const busyId =
    (setListing.isPending ? setListing.variables?.catalogId : undefined) ??
    (setFeatured.isPending ? setFeatured.variables?.catalogId : undefined) ??
    (setOfficial.isPending ? setOfficial.variables?.catalogId : undefined) ??
    null

  const total = query.data?.total ?? 0
  const rows = query.data?.items ?? []

  const goto = (path: string, key: string, entry: StoreEntry) =>
    navigate(`${path}?${key}=${encodeURIComponent(entry.catalog_id)}`)

  const confirm = (reason: string) => {
    if (!confirming) return
    setListing.mutate(
      {
        catalogId: confirming.entry.catalog_id,
        listing: confirming.action === "delist" ? "delisted" : "listed",
        note: reason,
      },
      { onSuccess: () => setConfirming(null) },
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4">
        <StoreFilters values={values} onChange={setValues} />
        <StoreTable
          rows={rows}
          isLoading={query.isPending}
          error={query.error}
          busyId={busyId}
          onInstalls={(entry) => goto(paths.adminSkills("installs"), "catalog", entry)}
          onView={(entry) => goto(paths.adminSkills("review"), "id", entry)}
          onRelist={(entry) => setConfirming({ action: "list", entry })}
          onDelist={(entry) => setConfirming({ action: "delist", entry })}
          onFeature={(entry) =>
            setFeatured.mutate({ catalogId: entry.catalog_id, featured: !entry.featured })
          }
          onOfficial={(entry) =>
            setOfficial.mutate({ catalogId: entry.catalog_id, isOfficial: !entry.is_official })
          }
        />
        {total > 0 && (
          <Pagination
            offset={offset}
            limit={LIMIT}
            total={total}
            onOffsetChange={(next) => setValues({ offset: String(next) })}
            labels={{
              previous: t("list.previous"),
              next: t("list.next"),
              nav: t("store.nav"),
              range: t("list.range", {
                from: offset + 1,
                to: Math.min(offset + LIMIT, total),
                total,
              }),
            }}
          />
        )}
      </section>

      <ListingDialog
        open={!!confirming}
        title={t(`dialog.${confirming?.action ?? "list"}.title`, {
          title: confirming?.entry.title ?? "",
        })}
        body={t(`dialog.${confirming?.action ?? "list"}.body`)}
        confirmLabel={t(confirming?.action === "delist" ? "action.delist" : "action.list")}
        // Delisting hides someone's published work; the reason is what they are
        // told, so it cannot be blank (plan §4.5.1).
        requireReason={confirming?.action === "delist"}
        pending={setListing.isPending}
        failed={setListing.isError}
        onClose={() => setConfirming(null)}
        onConfirm={confirm}
      />
    </div>
  )
}
