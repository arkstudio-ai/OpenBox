// 消息通知 › 专题 — topic list, inline editor, publish/unpublish, share link.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { formatDateTime } from "@/shared/lib/format"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { toast } from "@/shared/ui/Toast"
import { paths } from "@/shared/router/paths"
import { useTopics, useTopicWrites, type Topic, type TopicInput } from "../api"
import { topicShareUrl } from "../lib/share"
import { button, primary } from "../lib/styles"
import { TopicForm } from "./TopicForm"

export function TopicsPage() {
  const { t, i18n } = useTranslation(["admin-messages", "errors"])
  const query = useTopics()
  const writes = useTopicWrites()
  const [editing, setEditing] = useState<Topic | null | "new">(null)

  const errorText = (error: unknown) =>
    error instanceof ApiError && i18n.exists(error.code, { ns: "errors" })
      ? t(error.code, { ns: "errors" })
      : t("common.requestFailed")

  const save = async (body: TopicInput) => {
    if (editing === "new") await writes.create.mutateAsync(body)
    else if (editing) await writes.update.mutateAsync({ id: editing.id, body })
    toast("success", t("form.saved"))
    setEditing(null)
  }

  const flip = (row: Topic) =>
    void (row.status === "published" ? writes.unpublish : writes.publish)
      .mutateAsync(row.id)
      .then(() => toast("success", t("form.saved")))
      .catch((error) => toast("error", errorText(error)))

  const copyLink = (row: Topic) =>
    void navigator.clipboard
      .writeText(topicShareUrl(row.slug))
      .then(() => toast("success", t("topics.linkCopied")))
      .catch(() => toast("error", t("common.requestFailed")))

  const columns: DataTableColumn<Topic>[] = [
    {
      key: "title",
      header: t("topics.title"),
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-medium">{row.title}</p>
          <p className="text-n600 truncate font-mono text-xs">{paths.topic(row.slug)}</p>
        </div>
      ),
    },
    {
      key: "status",
      header: t("list.status"),
      render: (row) => (
        <div className="flex flex-col gap-1">
          <StatusPill tone={row.status === "published" ? "ok" : "muted"}>
            {t(`status.${row.status}`)}
          </StatusPill>
          <span className="text-n600 text-xs">
            {row.publishedAt
              ? t("topics.publishedAt", { time: formatDateTime(row.publishedAt) })
              : t("topics.updatedAt", { time: formatDateTime(row.updatedAt) })}
          </span>
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <div className="flex flex-wrap gap-1.5" onClick={(e) => e.stopPropagation()}>
          <button className={button} onClick={() => setEditing(row)}>
            {t("topics.editor")}
          </button>
          <button className={row.status === "published" ? button : primary} onClick={() => flip(row)}>
            {t(row.status === "published" ? "topics.unpublish" : "topics.publish")}
          </button>
          {row.status === "published" && (
            <>
              <a className={button} href={paths.topic(row.slug)} target="_blank" rel="noreferrer">
                {t("topics.view")}
              </a>
              <button className={button} onClick={() => copyLink(row)}>
                {t("topics.copyLink")}
              </button>
            </>
          )}
        </div>
      ),
    },
  ]

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {editing !== null ? (
        <TopicForm
          key={editing === "new" ? "new" : editing.id}
          source={editing === "new" ? null : editing}
          saving={writes.create.isPending || writes.update.isPending}
          onCancel={() => setEditing(null)}
          onSave={save}
        />
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-n600 text-sm">{t("subtitle")}</p>
          <div className="flex gap-2">
            <button className={button} onClick={() => void query.refetch()} disabled={query.isFetching}>
              {t("common.refresh")}
            </button>
            <button className={primary} onClick={() => setEditing("new")}>
              {t("topics.create")}
            </button>
          </div>
        </div>
      )}
      <DataTable
        columns={columns}
        rows={query.data?.items ?? []}
        rowKey={(row) => row.id}
        isLoading={query.isPending}
        error={query.error}
        emptyText={t("topics.empty")}
        errorText={t("common.loadFailed")}
        loadingLabel={t("common.refresh")}
        minWidth="min-w-[40rem]"
      />
    </div>
  )
}
