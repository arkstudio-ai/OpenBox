// 消息通知 › 公告 — the list, with the draft editor and the publish/revoke
// confirmations. Publishing fans out in the background; the list polls until
// the server stamps `fanoutAt`.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError, http } from "@/shared/api/http"
import { formatDateTime } from "@/shared/lib/format"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill, type StatusTone } from "@/shared/ui/StatusPill"
import { toast } from "@/shared/ui/Toast"
import { useAnnouncements, useAnnouncementWrites, type Announcement, type AnnouncementInput } from "../api"
import { button, primary } from "../lib/styles"
import { AnnouncementForm } from "./AnnouncementForm"
import { ConfirmAction, type PendingAction } from "./ConfirmAction"

const TONE: Record<Announcement["status"], StatusTone> = {
  draft: "muted",
  scheduled: "warn",
  published: "ok",
  revoked: "danger",
}

export function AnnouncementsPage() {
  const { t, i18n } = useTranslation(["admin-messages", "errors"])
  const query = useAnnouncements()
  const writes = useAnnouncementWrites()
  const [editing, setEditing] = useState<Announcement | null | "new">(null)
  const [pending, setPending] = useState<PendingAction | null>(null)

  const errorText = (error: unknown) =>
    error instanceof ApiError && i18n.exists(error.code, { ns: "errors" })
      ? t(error.code, { ns: "errors" })
      : t("common.requestFailed")

  const save = async (body: AnnouncementInput) => {
    if (editing === "new") await writes.create.mutateAsync(body)
    else if (editing) await writes.update.mutateAsync({ id: editing.id, body })
    toast("success", t("form.saved"))
    setEditing(null)
  }

  // The list omits recipientCount (it is a live query per audience); fetch
  // the single row so the confirmation says how many people it reaches.
  const askPublish = async (row: Announcement) => {
    let count = row.recipientCount ?? 0
    try {
      const detail = await http.get<Announcement>(
        `/api/admin/messages/announcements/${encodeURIComponent(row.id)}`,
      )
      count = detail.recipientCount ?? count
    } catch (error) {
      toast("error", errorText(error))
      return
    }
    setPending({
      title: t("action.publishConfirmTitle"),
      body: t("action.publishConfirmBody", { count }),
      run: () => {
        void writes.publish
          .mutateAsync(row.id)
          .then((result) =>
            toast("success", t(result.status === "scheduled" ? "action.scheduled" : "action.published")),
          )
          .catch((error) => toast("error", errorText(error)))
      },
    })
  }

  const askRevoke = (row: Announcement) =>
    setPending({
      title: t("action.revokeConfirmTitle"),
      body: t("action.revokeConfirmBody"),
      danger: true,
      run: () => {
        void writes.revoke
          .mutateAsync(row.id)
          .then((result) => toast("success", t("action.revoked", { count: result.hidden })))
          .catch((error) => toast("error", errorText(error)))
      },
    })

  const preview = (row: Announcement) =>
    void writes.preview
      .mutateAsync(row.id)
      .then(() => toast("success", t("action.previewSent")))
      .catch((error) => toast("error", errorText(error)))

  const audienceText = (row: Announcement) => {
    const a = row.audience
    switch (a.kind) {
      case "role":
        return t("list.audience.role", { role: t(a.role === "admin" ? "form.roleAdmin" : "form.roleUser") })
      case "workspace":
        return t("list.audience.workspace", { id: a.id })
      case "users":
        return t("list.audience.users", { count: a.ids.length })
      default:
        return t("list.audience.all")
    }
  }

  const columns: DataTableColumn<Announcement>[] = [
    {
      key: "title",
      header: t("form.title"),
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-medium">{row.title}</p>
          {row.body && <p className="text-n600 truncate text-xs">{row.body}</p>}
        </div>
      ),
    },
    {
      key: "status",
      header: t("list.status"),
      render: (row) => (
        <div className="flex flex-col gap-1">
          <StatusPill tone={TONE[row.status]}>{t(`status.${row.status}`)}</StatusPill>
          {row.status === "scheduled" && row.publishAt && (
            <span className="text-n600 text-xs">
              {t("list.publishAt", { time: formatDateTime(row.publishAt) })}
            </span>
          )}
          {row.status === "published" && (
            <span className="text-n600 text-xs">{t("list.fanout", { count: row.fanoutCount })}</span>
          )}
        </div>
      ),
    },
    {
      key: "audience",
      header: t("form.audience"),
      render: (row) => (
        <div className="text-xs">
          <p>{audienceText(row)}</p>
          <p className="text-n600">{t(row.push ? "list.push" : "list.noPush")}</p>
        </div>
      ),
    },
    {
      key: "link",
      header: t("form.link"),
      render: (row) =>
        row.link?.kind === "topic"
          ? t("list.link.topic", { slug: row.link.slug })
          : row.link?.kind === "url"
            ? t("list.link.url")
            : t("list.link.none"),
    },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <div className="flex flex-wrap gap-1.5" onClick={(e) => e.stopPropagation()}>
          {(row.status === "draft" || row.status === "scheduled") && (
            <>
              <button className={button} onClick={() => setEditing(row)}>
                {t("action.edit")}
              </button>
              <button className={primary} onClick={() => void askPublish(row)}>
                {t(
                  row.publishAt && Date.parse(row.publishAt) > Date.now()
                    ? "action.schedule"
                    : "action.publish",
                )}
              </button>
            </>
          )}
          {(row.status === "scheduled" || row.status === "published") && (
            <button className={button} onClick={() => askRevoke(row)}>
              {t("action.revoke")}
            </button>
          )}
          <button className={button} onClick={() => preview(row)} disabled={writes.preview.isPending}>
            {t("action.preview")}
          </button>
        </div>
      ),
    },
  ]

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-n600 text-sm">{t("subtitle")}</p>
        <div className="flex gap-2">
          <button className={button} onClick={() => void query.refetch()} disabled={query.isFetching}>
            {t("common.refresh")}
          </button>
          <button className={primary} onClick={() => setEditing("new")}>
            {t("list.create")}
          </button>
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={query.data?.items ?? []}
        rowKey={(row) => row.id}
        isLoading={query.isPending}
        error={query.error}
        emptyText={t("list.empty")}
        errorText={t("common.loadFailed")}
        loadingLabel={t("common.refresh")}
        minWidth="min-w-[56rem]"
      />
      {editing !== null && (
        <AnnouncementForm
          key={editing === "new" ? "new" : editing.id}
          open
          source={editing === "new" ? null : editing}
          saving={writes.create.isPending || writes.update.isPending}
          onClose={() => setEditing(null)}
          onSave={save}
        />
      )}
      <ConfirmAction pending={pending} onClose={() => setPending(null)} />
    </div>
  )
}
