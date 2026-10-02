import { useState } from "react"
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { paths } from "@/shared/router/paths"
import { useMemoryScope } from "../api"
import { workflowApi, type Profile, type ProfileDefinition } from "./workflow-api"
import { Field, JsonEditor, panel, ScopeHint, useWikiAction, WikiError } from "./platform-ui"

const outputKinds = ["page", "relation", "lifecycle", "artifact"]

export function WikiProfiles({ projectId, profiles }: { projectId: string; profiles: Profile[] }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [selected, setSelected] = useState<Profile>()
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState("")
  const template = useQuery({ queryKey: [...key, "wiki-profile-templates"], queryFn: workflowApi.templates })
  const save = useWikiAction(
    () =>
      workflowApi.saveProfile(
        selected ? (selected.project_id ?? "") : projectId,
        JSON.parse(text) as ProfileDefinition,
        selected,
      ),
    (result) => {
      setSelected(result)
      setEditing(false)
    },
  )
  return (
    <div className="space-y-5">
      <div className={panel}>
        <h2 className="text-xl font-semibold">{t("profiles")}</h2>
        <p className="text-n500 text-sm leading-6">{t("profilesHint")}</p>
        <ScopeHint projectId={projectId} />
        <button
          className={memoryPrimary}
          disabled={!template.data}
          onClick={() => {
            setSelected(undefined)
            setText(JSON.stringify(template.data!.templates[0], null, 2))
            setEditing(true)
          }}
        >
          {t("useProfileTemplate")}
        </button>
        <WikiError error={template.error ?? save.error} />
        {editing && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              save.mutate()
            }}
          >
            <JsonEditor label={t("profileDefinition")} value={text} onChange={setText} />
            <div className="flex gap-2">
              <button className={memoryPrimary} disabled={save.isPending}>
                {t("saveProfile")}
              </button>
              <button type="button" className={memoryButton} onClick={() => setEditing(false)}>
                {t("close")}
              </button>
            </div>
          </form>
        )}
        {!!profiles.length && (
          <Field label={t("chooseProfile")}>
            <select
              className={memoryInput}
              value={selected?.id ?? ""}
              onChange={(event) => {
                setSelected(profiles.find((profile) => profile.id === event.target.value))
                setEditing(false)
              }}
            >
              <option value="">{t("choose")}</option>
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.title}
                </option>
              ))}
            </select>
          </Field>
        )}
        {selected && (
          <button
            className={memoryButton}
            onClick={() => {
              setText(JSON.stringify(selected.definition, null, 2))
              setEditing(true)
            }}
          >
            {t("editProfile")}
          </button>
        )}
      </div>
      {selected && (
        <ProfileRecords
          key={selected.id + selected.revision}
          profile={profiles.find((item) => item.id === selected.id) ?? selected}
        />
      )}
    </div>
  )
}

function ProfileRecords({ profile }: { profile: Profile }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const records = useInfiniteQuery({
    queryKey: [...key, "wiki-records", profile.id],
    queryFn: ({ pageParam }) => workflowApi.records(profile.id, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const stats = useQuery({
    queryKey: [...key, "wiki-lifecycle-statistics", profile.id],
    queryFn: () => workflowApi.statistics(profile.id),
    refetchInterval: 10000,
  })
  const [kind, setKind] = useState("page")
  const [payload, setPayload] = useState("{}")
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const write = useWikiAction(() =>
    workflowApi.mutateRecord(profile, kind, JSON.parse(payload) as Record<string, unknown>),
  )
  return (
    <section className={panel}>
      <h3 className="font-semibold">{t("typedRecords")}</h3>
      <WikiError error={records.error ?? stats.error ?? write.error} />
      {stats.data && (
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(stats.data.entities).map(([type, counts]) => (
            <div key={type} className="border-hair rounded-lg border p-3 text-sm">
              <h4 className="mb-2 font-medium">
                {type} · {counts.total}
              </h4>
              <dl className="flex flex-wrap gap-3">
                {Object.entries(counts.states).map(([state, count]) => (
                  <div key={state}>
                    <dt className="text-n500">{state}</dt>
                    <dd>{count}</dd>
                  </div>
                ))}
              </dl>
              {!!counts.unreachable.length && (
                <p className="text-danger mt-2">
                  {t("unreachableStates", { states: counts.unreachable.join(", ") })}
                </p>
              )}
              {!!counts.unavailable && (
                <p className="text-n500 mt-2">{t("unavailableRecords", { count: counts.unavailable })}</p>
              )}
            </div>
          ))}
        </div>
      )}
      {records.data?.pages
        .flatMap((page) => page.records)
        .map((record) => (
          <article key={record.id} className="border-hair space-y-2 border-t pt-3 text-sm">
            <div className="flex justify-between gap-2">
              <h4 className="font-semibold">{record.title ?? t("stale")}</h4>
              <span className="text-n500">
                {record.entity_type} · {t("revision", { revision: record.revision })}
              </span>
            </div>
            {record.available && (
              <>
                <pre className="overflow-auto whitespace-pre-wrap">
                  {JSON.stringify(record.fields, null, 2)}
                </pre>
                <Link
                  className="text-a700 underline"
                  to={paths.wikiPage(record.page_id, profile.project_id ?? "")}
                >
                  {t("readPage")}
                </Link>
                <button
                  className={memoryButton + " ms-3"}
                  onClick={() => {
                    setKind("page")
                    setAdvancedOpen(true)
                    setPayload(
                      JSON.stringify(
                        {
                          entity_type: record.entity_type,
                          slug: record.slug,
                          title: record.title,
                          fields: record.fields,
                          page_id: record.page_id,
                          page_revision: record.page_revision,
                          expected_revision: record.revision,
                        },
                        null,
                        2,
                      ),
                    )
                  }}
                >
                  {t("editRecord")}
                </button>
              </>
            )}
          </article>
        ))}
      {records.hasNextPage && (
        <button className={memoryButton} onClick={() => void records.fetchNextPage()}>
          {t("loadMore")}
        </button>
      )}
      <details open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer text-sm font-medium">{t("advancedRecordWrite")}</summary>
        <form
          className="mt-4 space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            write.mutate()
          }}
        >
          <Field label={t("outputKind")}>
            <select className={memoryInput} value={kind} onChange={(event) => setKind(event.target.value)}>
              {outputKinds.map((value) => (
                <option key={value} value={value}>
                  {t("outputKinds." + value)}
                </option>
              ))}
            </select>
          </Field>
          <JsonEditor label={t("recordPayload")} value={payload} onChange={setPayload} />
          <button className={memoryPrimary} disabled={write.isPending}>
            {t("saveRecord")}
          </button>
        </form>
      </details>
    </section>
  )
}
