import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useParams, useSearchParams } from "react-router"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "../api"
import { WikiEditor } from "../wiki/WikiEditor"
import { WikiReader } from "../wiki/WikiReader"
import { useLibrary } from "./data"
import { KnowledgeHome } from "./KnowledgeHome"

/** /app/wiki and /app/wiki/:pageId — the knowledge home or one page of it.
 *  Remounted per user and workspace, so nothing read or drafted in one
 *  workspace survives into another. */
export function KnowledgeWorkspace() {
  const { userId, workspaceId } = useMemoryScope()
  return <KnowledgeShell key={`${userId}:${workspaceId}`} />
}

function KnowledgeShell() {
  const { pageId } = useParams()
  return (
    // Keyed so opening a page starts at its top rather than the list's offset.
    <div key={pageId ?? "home"} className="scr h-full min-w-0 overflow-y-auto">
      {pageId ? <TopicPage pageId={pageId} /> : <KnowledgeHome />}
    </div>
  )
}

function TopicPage({ pageId }: { pageId: string }) {
  const { t } = useTranslation("wiki")
  const [params] = useSearchParams()
  const projectId = params.get("project") ?? ""
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const library = useLibrary(projectId, "")
  const [editing, setEditing] = useState(false)
  const pages = library.error ? [] : (library.data?.pages.flatMap((batch) => batch.pages) ?? [])
  return (
    <>
      <WikiReader pageId={pageId} pages={pages} projectId={projectId} onEdit={() => setEditing(true)} />
      {editing && (
        <WikiEditor
          pageId={pageId}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            toast.success(t("consumer.saved"))
            void qc.invalidateQueries({ queryKey: key })
          }}
        />
      )}
    </>
  )
}
