import { useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import { MemoryStatus, memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { platformApi, type Concept } from "./platform-api"
import { Field, panel, useWikiAction, WikiError } from "./platform-ui"

export function WikiConcepts({ projectId }: { projectId: string }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [query, setQuery] = useState("")
  const [category, setCategory] = useState("")
  const [selected, setSelected] = useState<string | null>(null)
  const data = useInfiniteQuery({
    queryKey: [...key, "wiki-concepts", projectId, query, category],
    queryFn: ({ pageParam }) => platformApi.concepts(projectId, pageParam, query, category),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const concepts = data.error ? [] : (data.data?.pages.flatMap((page) => page.concepts) ?? [])
  const active = concepts.find((item) => item.id === selected)
  return (
    <section className="space-y-4" aria-label={t("concepts")}>
      <div>
        <h2 className="text-xl font-semibold">{t("concepts")}</h2>
        <p className="text-n500 mt-2 text-sm">{t("conceptsHint")}</p>
      </div>
      <div className="flex flex-wrap gap-3">
        <Field label={t("search")}>
          <input
            className={memoryInput}
            value={query}
            maxLength={200}
            onChange={(event) => setQuery(event.target.value)}
          />
        </Field>
        <Field label={t("category")}>
          <input
            className={memoryInput}
            value={category}
            maxLength={80}
            onChange={(event) => setCategory(event.target.value)}
          />
        </Field>
      </div>
      <WikiError error={data.error} />
      {!concepts.length && <p className={panel}>{t(data.isPending ? "loading" : "noConcepts")}</p>}
      <div className="grid gap-3 xl:grid-cols-2">
        {concepts.map((concept) => (
          <article key={concept.id} className={panel}>
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-semibold">{concept.title ?? t("stale")}</h3>
              <MemoryStatus status={concept.status} />
            </div>
            {concept.body_available && (
              <>
                <p className="text-n500 text-xs">
                  {concept.category} · {concept.aliases.join(" · ")}
                </p>
                <p className="text-sm leading-6">{concept.description}</p>
                <p className="text-n500 text-xs">
                  {t("conceptSourceCount", { count: concept.memory_ids.length })}
                </p>
                <details className="text-sm">
                  <summary className="cursor-pointer">{t("evidence")}</summary>
                  {concept.evidence.map((item, index) => (
                    <blockquote key={index} className="border-hair my-3 border-s-2 ps-3">
                      {item.quote}
                    </blockquote>
                  ))}
                </details>
                <div className="flex flex-wrap gap-3">
                  <button className={memoryButton} onClick={() => setSelected(concept.id)}>
                    {t("editConcept")}
                  </button>
                  {concept.page_id && concept.page_available && (
                    <Link
                      className={memoryButton}
                      to={paths.wikiPage(concept.page_id, concept.project_id ?? "")}
                    >
                      {t("readPage")}
                    </Link>
                  )}
                  {concept.page_id && !concept.page_available && <Link className={memoryButton}
                    to={paths.wiki(concept.project_id ?? "") + (concept.project_id ? "&" : "?") + "view=reviews"}>{t("openReviews")}</Link>}
                </div>
              </>
            )}
          </article>
        ))}
      </div>
      {data.hasNextPage && (
        <button
          className={memoryButton}
          disabled={data.isFetchingNextPage}
          onClick={() => void data.fetchNextPage()}
        >
          {t("loadMore")}
        </button>
      )}
      {active?.body_available && (
        <ConceptEditor
          key={active.id + active.revision}
          concept={active}
          concepts={concepts}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  )
}

function ConceptEditor({
  concept,
  concepts,
  onClose,
}: {
  concept: Concept
  concepts: Concept[]
  onClose: () => void
}) {
  const { t } = useTranslation("wiki")
  const [title, setTitle] = useState(concept.title ?? "")
  const [aliases, setAliases] = useState(concept.aliases.join("\n"))
  const [category, setCategory] = useState(concept.category ?? "")
  const [description, setDescription] = useState(concept.description ?? "")
  const [target, setTarget] = useState("")
  const edit = useWikiAction(
    () =>
      platformApi.editConcept(concept, {
        title,
        aliases: aliases
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
        category,
        description,
      }),
    onClose,
  )
  const merge = useWikiAction(
    () =>
      platformApi.merge(
        concept,
        concepts.find((item) => item.id === target)!,
      ),
    onClose,
  )
  const choices = concepts.filter(
    (item) => item.id !== concept.id && item.body_available && item.project_id === concept.project_id,
  )
  return (
    <section className={panel} aria-label={t("editConcept")}>
      <h3 className="font-semibold">{t("editConcept")}</h3>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          edit.mutate()
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t("pageTitle")}>
            <input
              required
              maxLength={160}
              className={memoryInput}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </Field>
          <Field label={t("category")}>
            <input
              required
              maxLength={80}
              className={memoryInput}
              value={category}
              onChange={(event) => setCategory(event.target.value)}
            />
          </Field>
        </div>
        <Field label={t("aliases")}>
          <textarea
            className={memoryInput}
            value={aliases}
            onChange={(event) => setAliases(event.target.value)}
          />
        </Field>
        <Field label={t("description")}>
          <textarea
            maxLength={800}
            className={memoryInput}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <WikiError error={edit.error ?? merge.error} />
        <div className="flex gap-2">
          <button className={memoryPrimary} disabled={edit.isPending}>
            {t("save")}
          </button>
          <button type="button" className={memoryButton} onClick={onClose}>
            {t("close")}
          </button>
        </div>
      </form>
      <div className="border-hair space-y-3 border-t pt-4">
        <p className="text-n500 text-sm">{t("mergeHint")}</p>
        <Field label={t("mergeTarget")}>
          <select className={memoryInput} value={target} onChange={(event) => setTarget(event.target.value)}>
            <option value="">{t("choose")}</option>
            {choices.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </Field>
        <button className={memoryButton} disabled={!target || merge.isPending} onClick={() => merge.mutate()}>
          {t("mergeConcept")}
        </button>
      </div>
    </section>
  )
}
