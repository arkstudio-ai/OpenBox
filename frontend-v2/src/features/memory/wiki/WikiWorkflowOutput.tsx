import { useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi, type WikiSummary } from "../wiki-api"
import { workflowApi, type StageDefinition, type TypedRecord, type WorkflowRun } from "./workflow-api"
import { Field, JsonEditor, WikiError } from "./platform-ui"
import { WikiFields } from "./WikiFields"

export function WikiWorkflowOutput({
  run,
  stage,
  pending,
  onSubmit,
}: {
  run: WorkflowRun
  stage: StageDefinition
  pending: boolean
  onSubmit: (outputs: Record<string, unknown>[]) => void
}) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [draft, setDraft] = useState(JSON.stringify(run.stages[stage.id].outputs, null, 2))
  const [error, setError] = useState<unknown>()
  const pages = useInfiniteQuery({
    queryKey: [...key, "wiki-output-pages", run.project_id],
    queryFn: ({ pageParam }) =>
      wikiApi.library({ projectId: run.project_id ?? "", query: "", status: "published", offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const records = useInfiniteQuery({
    queryKey: [...key, "wiki-records", run.profile_id],
    queryFn: ({ pageParam }) => workflowApi.records(run.profile_id, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const allRecords =
    records.data?.pages.flatMap((page) => page.records).filter((record) => record.available) ?? []
  const add = (value: Record<string, unknown>) => {
    try {
      const items = JSON.parse(draft) as unknown
      if (!Array.isArray(items)) throw new Error(t("invalidJson"))
      setDraft(JSON.stringify([...items, value], null, 2))
      setError(undefined)
    } catch (cause) {
      setError(cause)
    }
  }
  return (
    <details open className="border-hair space-y-4 border-t pt-4">
      <summary className="cursor-pointer font-semibold">{t("stageOutputs")}</summary>
      <p className="text-n500 text-sm">{t("stageOutputsHint", { count: stage.outputsRequired ?? 0 })}</p>
      <OutputBuilder
        run={run}
        stage={stage}
        pages={
          pages.data?.pages
            .flatMap((batch) => batch.pages)
            .filter((page) => page.body_available && page.project_id === run.project_id) ?? []
        }
        records={allRecords}
        onAdd={add}
      />
      {pages.hasNextPage && (
        <button className={memoryButton} onClick={() => void pages.fetchNextPage()}>
          {t("morePages")}
        </button>
      )}
      {records.hasNextPage && (
        <button className={memoryButton} onClick={() => void records.fetchNextPage()}>
          {t("moreRecords")}
        </button>
      )}
      <JsonEditor label={t("stagedOutputs")} value={draft} onChange={setDraft} />
      <WikiError error={error ?? pages.error ?? records.error} />
      <button
        className={memoryPrimary}
        disabled={pending}
        onClick={() => {
          try {
            const values = JSON.parse(draft) as Record<string, unknown>[]
            if (!Array.isArray(values)) throw new Error(t("invalidJson"))
            onSubmit(values)
            setError(undefined)
          } catch (cause) {
            setError(cause)
          }
        }}
      >
        {t("submitOutputs")}
      </button>
    </details>
  )
}

function useOutputBuilder({
  run,
  stage,
  pages,
  records,
  onAdd,
}: {
  run: WorkflowRun
  stage: StageDefinition
  pages: WikiSummary[]
  records: TypedRecord[]
  onAdd: (output: Record<string, unknown>) => void
}) {
  const { t } = useTranslation("wiki")
  const initialKind = stage.writes.length ? "page" : stage.relationWrites?.length ? "relation" : "artifact"
  const [kind, setKind] = useState(initialKind)
  const [type, setType] = useState(
    (initialKind === "page"
      ? stage.writes
      : initialKind === "relation"
        ? stage.relationWrites
        : stage.artifactWrites)?.[0] ?? "",
  )
  const [pageId, setPageId] = useState("")
  const [recordId, setRecordId] = useState("")
  const [otherId, setOtherId] = useState("")
  const [target, setTarget] = useState("")
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [body, setBody] = useState("")
  const [name, setName] = useState("")
  const [media, setMedia] = useState("text/markdown")
  const page = pages.find((item) => item.id === pageId)
  const record = records.find((item) => item.id === recordId)
  const other = records.find((item) => item.id === otherId)
  const entity = run.definition.entities[type]
  const fields =
    kind === "relation" ? (run.definition.relations[type]?.attributes ?? {}) : (entity?.fields ?? {})
  const lifecycle = record ? run.definition.entities[record.entity_type]?.lifecycle : undefined
  const options =
    kind === "relation"
      ? (stage.relationWrites ?? [])
      : kind === "artifact"
        ? (stage.artifactWrites ?? [])
        : stage.writes
  const kinds = [
    ...(stage.writes.length ? ["page", "lifecycle"] : []),
    ...(stage.relationWrites?.length ? ["relation"] : []),
    ...(stage.artifactWrites?.length ? ["artifact"] : []),
  ]
  const ready = () =>
    kind === "page"
      ? !!page && !!type
      : kind === "lifecycle"
        ? !!record && !!target
        : kind === "relation"
          ? !!record && !!other && !!type
          : !!record && !!body && !!name && !!type
  const build = () => {
    if (kind === "page")
      return {
        kind,
        entity_type: type,
        slug: record?.slug ?? page!.slug,
        title: page!.title,
        fields: values,
        page_id: page!.id,
        page_revision: page!.revision,
        expected_revision: record?.revision ?? 0,
      }
    if (kind === "lifecycle")
      return { kind, record_id: record!.id, expected_revision: record!.revision, to: target }
    if (kind === "relation")
      return {
        kind,
        type,
        from_id: record!.id,
        from_revision: record!.revision,
        to_id: other!.id,
        to_revision: other!.revision,
        attributes: values,
        expected_revision: 0,
      }
    return {
      kind,
      type,
      name,
      media_type: media,
      body,
      records: [{ id: record!.id, revision: record!.revision }],
    }
  }
  const changeKind = (next: string) => {
    setKind(next)
    setType(
      (next === "relation"
        ? stage.relationWrites
        : next === "artifact"
          ? stage.artifactWrites
          : stage.writes)?.[0] ?? "",
    )
    setRecordId("")
    setOtherId("")
    setValues({})
  }
  return {
    t,
    kind,
    type,
    pageId,
    recordId,
    otherId,
    target,
    values,
    body,
    name,
    media,
    entity,
    fields,
    lifecycle,
    options,
    kinds,
    ready,
    build,
    changeKind,
    setType,
    setPageId,
    setRecordId,
    setOtherId,
    setTarget,
    setValues,
    setBody,
    setName,
    setMedia,
    run,
    stage,
    pages,
    records,
    record,
    onAdd,
  }
}

function OutputBuilder(props: Parameters<typeof useOutputBuilder>[0]) {
  const {
    t,
    kind,
    type,
    pageId,
    recordId,
    otherId,
    target,
    values,
    body,
    name,
    media,
    entity,
    fields,
    lifecycle,
    options,
    kinds,
    ready,
    build,
    changeKind,
    setType,
    setPageId,
    setRecordId,
    setOtherId,
    setTarget,
    setValues,
    setBody,
    setName,
    setMedia,
    run,
    pages,
    records,
    record,
    onAdd,
  } = useOutputBuilder(props)
  if (!kinds.length) return null
  return (
    <form
      className="bg-rail space-y-4 rounded-xl p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (ready()) onAdd(build())
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("outputKind")}>
          <select className={memoryInput} value={kind} onChange={(event) => changeKind(event.target.value)}>
            {kinds.map((value) => (
              <option key={value} value={value}>
                {t("outputKinds." + value)}
              </option>
            ))}
          </select>
        </Field>
        {kind !== "lifecycle" && (
          <Field label={t("entityType")}>
            <select
              className={memoryInput}
              value={type}
              onChange={(event) => {
                setType(event.target.value)
                setValues({})
                setRecordId("")
              }}
            >
              {options.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      {kind === "page" && (
        <Field label={t("choosePublishedPage")}>
          <select
            required
            className={memoryInput}
            value={pageId}
            onChange={(event) => setPageId(event.target.value)}
          >
            <option value="">{t("choose")}</option>
            {pages.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label={t(kind === "page" ? "updateRecordOptional" : "chooseRecord")}>
        <select
          required={kind !== "page"}
          className={memoryInput}
          value={recordId}
          onChange={(event) => {
            setRecordId(event.target.value)
            const item = records.find((row) => row.id === event.target.value)
            if (kind === "page") setValues(item?.fields ?? {})
            setTarget("")
          }}
        >
          <option value="">{t(kind === "page" ? "newTypedRecord" : "choose")}</option>
          {records
            .filter((item) => kind !== "page" || item.entity_type === type)
            .map((item) => (
              <option key={item.id} value={item.id}>
                {item.title} · {item.entity_type}
              </option>
            ))}
        </select>
      </Field>
      {kind === "lifecycle" && (
        <Field label={t("nextState")}>
          <select
            required
            className={memoryInput}
            value={target}
            onChange={(event) => setTarget(event.target.value)}
          >
            <option value="">{t("choose")}</option>
            {lifecycle?.transitions[String(record?.fields[lifecycle.field])]?.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </Field>
      )}
      {kind === "relation" && (
        <Field label={t("relatedRecord")}>
          <select
            required
            className={memoryInput}
            value={otherId}
            onChange={(event) => setOtherId(event.target.value)}
          >
            <option value="">{t("choose")}</option>
            {records.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </Field>
      )}
      {["page", "relation"].includes(kind) && (
        <WikiFields
          definitions={fields}
          values={values}
          onChange={setValues}
          omit={kind === "page" && entity?.lifecycle ? [entity.lifecycle.field] : []}
        />
      )}
      {kind === "artifact" && (
        <>
          <Field label={t("artifactName")}>
            <input
              required
              maxLength={160}
              className={memoryInput}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label={t("artifactFormat")}>
            <select className={memoryInput} value={media} onChange={(event) => setMedia(event.target.value)}>
              {run.definition.artifacts[type]?.mediaTypes.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("artifactContent")}>
            <textarea
              required
              maxLength={64000}
              className={memoryInput + " min-h-32"}
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
          </Field>
        </>
      )}
      <button className={memoryButton} disabled={!ready()}>
        {t("addOutput")}
      </button>
    </form>
  )
}
