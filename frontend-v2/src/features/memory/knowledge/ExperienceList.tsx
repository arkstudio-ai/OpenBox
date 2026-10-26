import { useState } from "react"
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { http } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useMemoryScope } from "../api"
import { button, card, field, primaryButton } from "./ui"

type Procedure = {
  title: string
  conditions: string
  steps: string[]
  expected_result: string
  failure_boundaries: string
  test_plan: string
}
type Experience = {
  id: string
  revision: number
  state: "candidate" | "validated" | "enabled" | "retired" | "needs_review"
  content: Procedure | null
  sources_available: boolean
  validation: { passed?: boolean; notes?: string }
}
const endpoint = "/api/memories/experiences"

export function ExperienceList({ projectId, query }: { projectId: string; query: string }) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const queryKey = [...key, "experiences", projectId]
  const list = useInfiniteQuery({
    queryKey,
    initialPageParam: 0,
    queryFn: ({ pageParam }) => http.get<{ items: Experience[]; next_offset: number | null }>(
      endpoint + "?" + new URLSearchParams({ offset: String(pageParam), ...(projectId ? { project_id: projectId } : {}) })),
    getNextPageParam: (page) => page.next_offset ?? undefined,
  })
  const items = list.data?.pages.flatMap((page) => page.items).filter((item) =>
    !query || JSON.stringify(item.content ?? {}).toLocaleLowerCase().includes(query.toLocaleLowerCase())) ?? []
  return <section className="space-y-4">
    <p className="text-n600 text-sm">{t("experience.hint")}</p>
    {list.isPending && <p role="status">{t("loading")}</p>}
    {list.error && <p role="alert">{errorText(list.error)}</p>}
    {!list.isPending && !list.error && !items.length && <p className="text-n600">{t("experience.empty")}</p>}
    {items.map((item) => <ExperienceCard key={`${item.id}:${item.revision}:${item.sources_available}`} item={item}
      changed={() => qc.invalidateQueries({ queryKey })} />)}
    {list.hasNextPage && <button className={button} disabled={list.isFetchingNextPage}
      onClick={() => void list.fetchNextPage()}>{t("loadMore")}</button>}
  </section>
}

function ExperienceCard({ item, changed }: { item: Experience; changed: () => Promise<unknown> }) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const [notes, setNotes] = useState("")
  const [passed, setPassed] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(item.content)
  const write = useMutation({
    mutationFn: (action: "review" | "enable" | "retire" | "edit") => action === "edit"
      ? http.put(endpoint + "/" + item.id, { expected_revision: item.revision, content: draft })
      : http.post(endpoint + "/" + item.id + "/" + action, {
        expected_revision: item.revision, ...(action === "review" ? { passed, notes } : {}),
      }),
    onSuccess: changed,
  })
  const content = item.sources_available ? item.content : null
  return <article className={`${card} p-5 space-y-4`}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-lg font-medium break-words">{content?.title ?? t("experience.unavailable")}</h2>
      <span className="text-n600 text-sm">{t(`experience.state.${item.state}`)} · {t("experience.version", { version: item.revision })}</span>
    </div>
    {content && !editing && <dl className="space-y-3 text-sm leading-relaxed">
      {(["conditions", "steps", "expected_result", "failure_boundaries", "test_plan"] as const).map((name) =>
        <div key={name}><dt className="font-medium">{t(`experience.field.${name}`)}</dt>
          <dd className="text-n600 mt-1 whitespace-pre-wrap break-words">{name === "steps"
            ? content.steps.map((step, i) => `${i + 1}. ${step}`).join("\n") : content[name]}</dd></div>)}
    </dl>}
    {content && editing && draft && <div className="space-y-3">
      {(["title", "conditions", "steps", "expected_result", "failure_boundaries", "test_plan"] as const).map((name) =>
        <label className="block text-sm" key={name}>{t(`experience.field.${name}`)}
          <textarea className={`${field} mt-1`} rows={name === "steps" ? 4 : 2} maxLength={name === "steps" ? 8000 : name === "title" ? 120 : 1000}
            value={name === "steps" ? draft.steps.join("\n") : draft[name]}
            onChange={(e) => setDraft({ ...draft, [name]: name === "steps" ? e.target.value.split("\n") : e.target.value })} />
        </label>)}
      <p className="text-n600 text-sm">{t("experience.editHint")}</p>
      <button className={primaryButton} disabled={write.isPending} onClick={() => write.mutate("edit")}>{t("experience.saveDraft")}</button>
    </div>}
    {item.validation.notes && <p className="text-n600 whitespace-pre-wrap text-sm">{t("experience.lastReview")}：{item.validation.notes}</p>}
    {content && !editing && ["candidate", "validated"].includes(item.state) && <div className="space-y-3 border-t border-hair pt-4">
      <label className="block text-sm">{t("experience.reviewNotes")}
        <textarea className={`${field} mt-1`} value={notes} maxLength={3000} rows={3}
          placeholder={t("experience.reviewPlaceholder")} onChange={(e) => setNotes(e.target.value)} /></label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={passed}
        onChange={(e) => setPassed(e.target.checked)} />{t("experience.reviewPassed")}</label>
      <button className={button} disabled={write.isPending || notes.trim().length < 10}
        onClick={() => write.mutate("review")}>{t("experience.recordReview")}</button>
    </div>}
    <div className="flex flex-wrap gap-2">
      {content && <button className={button} disabled={write.isPending} onClick={() => setEditing(!editing)}>{t(editing ? "experience.cancelEdit" : "experience.edit")}</button>}
      {!editing && item.state === "validated" && <button className={primaryButton} disabled={write.isPending}
        onClick={() => write.mutate("enable")}>{t("experience.enable")}</button>}
      {item.state === "enabled" && <button className={button} disabled={write.isPending}
        onClick={() => write.mutate("retire")}>{t("experience.retire")}</button>}
    </div>
    {write.error && <p role="alert" className="text-dangerink text-sm">{errorText(write.error)}</p>}
  </article>
}
