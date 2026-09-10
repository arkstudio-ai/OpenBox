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
import { StoreEditor } from "./StoreEditor"
import { StoreUpload } from "./StoreUpload"
import { useDeleteEntries, useRestoreEntry } from "../api/management"

const LIMIT = 20
const DEFAULTS = { q: "", origin: ANY, kind: ANY, listing: ANY, offset: "0", deleted: "" }
const BUTTON = "rounded-full border border-hair px-4 py-2 text-sm hover:bg-hairsoft disabled:opacity-40"

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
  const [editor, setEditor] = useState<{ id: string | null } | null>(null)
  const [upload, setUpload] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [deleting, setDeleting] = useState<string[] | null>(null)
  const [feedback, setFeedback] = useState("")
  const remove = useDeleteEntries()
  const restore = useRestoreEntry()

  const offset = Number(values.offset) || 0
  const query = useStoreEntries({
    q: values.q,
    origin: values.origin,
    kind: values.kind,
    listing: values.listing,
    offset,
    limit: LIMIT,
    deleted: values.deleted || undefined,
  })
  const setListing = useSetListing()
  const setFeatured = useSetFeatured()
  const setOfficial = useSetOfficial()

  const busyId =
    [setListing, setFeatured, setOfficial].find((mutation) => mutation.isPending)?.variables?.catalogId ??
    null

  const total = query.data?.total ?? 0
  const rows = query.data?.items ?? []
  const selectedIds = rows.filter((row) => selected.has(row.catalog_id)).map((row) => row.catalog_id)
  const disabled = [query.isFetching, remove.isPending, restore.isPending, Boolean(busyId)].some(Boolean)
  const change = (patch: Partial<typeof values>) => {
    setSelected(new Set())
    setValues(patch)
  }
  const deleteSelected = async (reason: string) => {
    if (!deleting) return
    const result = await remove.mutateAsync({ catalog_ids: deleting, reason })
    const failures = result.items.filter((item) => !item.ok)
    setSelected(new Set(failures.map((item) => item.catalog_id)))
    setFeedback(
      t("manage.deleteResult", { success: result.items.length - failures.length, failed: failures.length }) +
        (failures.length ? ` ${failures.map((item) => `${item.catalog_id}: ${item.error}`).join("; ")}` : ""),
    )
    setDeleting(null)
  }

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
        <div className="flex flex-wrap items-center gap-2">
          <button className={BUTTON} onClick={() => setEditor({ id: null })}>
            {t("manage.create")}
          </button>
          <button className={BUTTON} onClick={() => setUpload(true)}>
            {t("manage.upload")}
          </button>
          <button
            className={BUTTON}
            aria-pressed={!!values.deleted}
            onClick={() => change({ deleted: values.deleted ? "" : "true", listing: ANY })}
          >
            {t("manage.trash")}
          </button>
          {!values.deleted && (
            <>
              <button
                className={BUTTON}
                disabled={disabled || !rows.length}
                onClick={() =>
                  setSelected(
                    selectedIds.length === rows.length
                      ? new Set()
                      : new Set(rows.map((row) => row.catalog_id)),
                  )
                }
              >
                {t("manage.selectPage")}
              </button>
              <button
                className={`${BUTTON} text-danger`}
                disabled={disabled || !selectedIds.length}
                onClick={() => {
                  remove.reset()
                  setDeleting(selectedIds)
                }}
              >
                {t("manage.deleteSelected", { count: selectedIds.length })}
              </button>
            </>
          )}
        </div>
        <StoreFilters values={values} onChange={change} />
        {feedback && (
          <p role="status" className="text-n700 text-sm break-words">
            {feedback}
          </p>
        )}
        {[setFeatured.isError, setOfficial.isError, restore.isError].some(Boolean) && (
          <p role="alert" className="text-danger text-sm">
            {t("dialog.failed")}
          </p>
        )}
        <StoreTable
          rows={rows}
          isLoading={query.isPending}
          error={query.error}
          busyId={busyId}
          disabled={disabled}
          selected={selected}
          onSelect={(id) =>
            setSelected((previous) => {
              const next = new Set(previous)
              if (next.has(id)) next.delete(id)
              else next.add(id)
              return next
            })
          }
          onEdit={(entry) => setEditor({ id: entry.catalog_id })}
          onDelete={(entry) => {
            remove.reset()
            setDeleting([entry.catalog_id])
          }}
          onRestore={(entry) =>
            restore.mutate(entry.catalog_id, { onSuccess: () => setFeedback(t("manage.restored")) })
          }
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
            onOffsetChange={(next) => change({ offset: String(next) })}
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

      {editor && <StoreEditor id={editor.id} onClose={() => setEditor(null)} />}
      {upload && <StoreUpload onClose={() => setUpload(false)} />}
      <ListingDialog
        open={!!deleting}
        title={t("manage.deleteTitle", { count: deleting?.length ?? 0 })}
        body={t("manage.deleteHint")}
        confirmLabel={t("manage.delete")}
        requireReason
        pending={remove.isPending}
        failed={remove.isError}
        onClose={() => {
          if (!remove.isPending) setDeleting(null)
        }}
        onConfirm={(reason) => {
          void deleteSelected(reason).catch(() => {})
        }}
      />

      <ShelfDialog
        confirming={confirming}
        mutation={setListing}
        onClose={() => setConfirming(null)}
        onConfirm={confirm}
      />
    </div>
  )
}

function ShelfDialog({
  confirming,
  mutation,
  onClose,
  onConfirm,
}: {
  confirming: Confirming | null
  mutation: ReturnType<typeof useSetListing>
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const { t } = useTranslation("admin-skills")
  return (
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
      pending={mutation.isPending}
      failed={mutation.isError}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  )
}
