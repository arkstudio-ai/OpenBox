import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import {
  DiagnosticData,
  MemoryStatus,
  memoryButton,
  memoryCard,
  memoryInput,
  memoryPrimary,
} from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "./api"
import { wikiApi, type WikiCandidate } from "./wiki-api"

function WikiCompile({
  projectId,
  model,
  enabled,
  onQueued,
}: {
  projectId: string
  model?: string
  enabled: boolean
  onQueued: (id?: string) => void
}) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const [title, setTitle] = useState("")
  const [slug, setSlug] = useState("")
  const [consent, setConsent] = useState(false)
  const requestId = useRef<string | null>(null)
  const mutation = useMutation({
    mutationFn: () => {
      requestId.current ??= crypto.randomUUID()
      return wikiApi.compile(slug, title.trim(), projectId, requestId.current)
    },
    onSuccess: (result) => {
      requestId.current = null
      setConsent(false)
      onQueued(result.id)
    },
  })
  const changeTitle = (value: string) => {
    setTitle(value)
    requestId.current = null
    setConsent(false)
  }
  const changeSlug = (value: string) => {
    setSlug(value)
    requestId.current = null
    setConsent(false)
  }
  const valid = title.trim() && /^[a-z0-9][a-z0-9_-]{0,79}$/.test(slug)
  return (
    <details className="border-hair rounded-lg border p-3">
      <summary className="cursor-pointer text-sm font-medium">{t("wiki.compileTitle")}</summary>
      <form
        className="mt-3 space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (enabled && valid && consent && !mutation.isPending) mutation.mutate()
        }}
      >
        <p className="text-n600 text-xs leading-relaxed">
          {t("wiki.compileHint", { scope: projectId || t("personal"), model: model ?? t("unknown") })}
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1.5 text-xs">
            <span>{t("wiki.pageTitle")}</span>
            <input
              className={memoryInput}
              value={title}
              onChange={(event) => changeTitle(event.target.value)}
              maxLength={160}
              disabled={mutation.isPending}
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs">
            <span>{t("wiki.slug")}</span>
            <input
              className={memoryInput}
              value={slug}
              onChange={(event) => changeSlug(event.target.value)}
              maxLength={80}
              pattern={"[a-z0-9][a-z0-9_\\-]{0,79}"}
              disabled={mutation.isPending}
            />
          </label>
        </div>
        <p className="text-n500 text-xs">{t("wiki.slugHint")}</p>
        <label className="flex items-start gap-2 text-sm leading-relaxed">
          <input
            type="checkbox"
            checked={consent}
            disabled={!enabled || mutation.isPending}
            onChange={(event) => setConsent(event.target.checked)}
            className="mt-1"
          />
          {t("wiki.compileConsent")}
        </label>
        <button
          className={memoryPrimary}
          disabled={!enabled || !valid || !consent || mutation.isPending}
          type="submit"
        >
          {t(mutation.isPending ? "saving" : "wiki.compile")}
        </button>
        {mutation.error && (
          <p role="alert" className="text-danger text-sm">
            {errorText(mutation.error)}
          </p>
        )}
        {mutation.data?.status === "unchanged" && (
          <p role="status" className="text-s800 text-sm">
            {t("wiki.unchanged")}
          </p>
        )}
      </form>
    </details>
  )
}

function WikiCandidateCard({
  candidate,
  pending,
  onDecision,
}: {
  candidate: WikiCandidate
  pending: boolean
  onDecision: (action: "approve" | "reject", candidate: WikiCandidate) => void
}) {
  const { t } = useTranslation("memory")
  return (
    <article className="border-hair space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">{candidate.title}</h3>
        <MemoryStatus status={candidate.status} />
      </div>
      <p className="text-n500 text-xs break-all">
        {candidate.slug} · {t("revision", { revision: candidate.revision })} · {candidate.model}
      </p>
      {candidate.body_available ? (
        <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{candidate.body}</p>
      ) : (
        <p className="text-n500 text-sm">{t("cannotReconstruct")}</p>
      )}
      {candidate.reason_code && <p className="text-n500 text-xs">{candidate.reason_code}</p>}
      <details>
        <summary className="text-n600 cursor-pointer text-xs">{t("wiki.evidence")}</summary>
        <DiagnosticData
          data={{
            candidate_hash: candidate.candidate_hash,
            expected_target_revision: candidate.expected_target_revision,
            expected_target_hash: candidate.expected_target_hash,
            sources: candidate.body_available ? candidate.sources : [],
            paragraphs: candidate.body_available ? candidate.paragraphs : [],
            usage: candidate.usage,
          }}
        />
      </details>
      {candidate.status === "pending" && (
        <div className="flex flex-wrap gap-2">
          <button
            className={memoryPrimary}
            disabled={pending || !candidate.body_available}
            onClick={() => onDecision("approve", candidate)}
          >
            {t("wiki.approve")}
          </button>
          <button className={memoryButton} disabled={pending} onClick={() => onDecision("reject", candidate)}>
            {t("reject")}
          </button>
        </div>
      )}
    </article>
  )
}

export function WikiPanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const { userId, workspaceId, key } = useMemoryScope()
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | undefined>()
  const capabilities = useQuery({ queryKey: [...key, "wiki-capabilities"], queryFn: wikiApi.capabilities })
  const pages = useQuery({
    queryKey: [...key, "wiki-pages", projectId],
    queryFn: () => wikiApi.pages(projectId),
  })
  const candidates = useQuery({
    queryKey: [...key, "wiki-candidates", projectId],
    queryFn: () => wikiApi.candidates(projectId),
  })
  const job = useQuery({
    queryKey: [...key, "wiki-job", jobId],
    queryFn: () => wikiApi.job(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      ["pending", "running", "retry"].includes(query.state.data?.status ?? "") ? 2000 : false,
  })
  const decide = useMutation({
    mutationFn: ({ action, candidate }: { action: "approve" | "reject"; candidate: WikiCandidate }) =>
      action === "approve" ? wikiApi.approve(candidate) : wikiApi.reject(candidate),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: key })
    },
  })
  const status = job.data?.status
  useEffect(() => {
    if (status === "completed") void qc.invalidateQueries({ queryKey: ["memory", userId, workspaceId] })
  }, [status, qc, userId, workspaceId])
  const error = capabilities.error ?? pages.error ?? candidates.error ?? job.error ?? decide.error
  const enabled = capabilities.data?.enabled === true
  return (
    <section className={`${memoryCard} space-y-4`} aria-label={t("wiki.title")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">{t("wiki.title")}</h2>
        <button
          className={memoryButton}
          onClick={() => {
            void pages.refetch()
            void candidates.refetch()
            if (jobId) void job.refetch()
          }}
        >
          {t("refresh")}
        </button>
      </div>
      <p className="text-n600 text-sm leading-relaxed">{t("wiki.hint")}</p>
      {!enabled && <p className="text-n500 text-xs">{t("wiki.disabled")}</p>}
      {error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(error)}
        </p>
      )}
      <WikiCompile
        key={projectId}
        projectId={projectId}
        enabled={enabled}
        model={capabilities.data?.model}
        onQueued={setJobId}
      />
      {job.data && (
        <div className="space-y-2">
          <MemoryStatus status={job.data.status} />
          <DiagnosticData data={job.data} />
          <Link
            className={memoryButton}
            to={paths.memoryDebug(`request_id=${encodeURIComponent(`wiki:${jobId}`)}`)}
          >
            {t("inspectTrace")}
          </Link>
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="space-y-3">
          <h3 className="text-sm font-medium">{t("wiki.candidates")}</h3>
          {(candidates.data?.candidates ?? []).map((candidate) => (
            <WikiCandidateCard
              key={candidate.id}
              candidate={candidate}
              pending={decide.isPending || !enabled}
              onDecision={(action, candidate) => decide.mutate({ action, candidate })}
            />
          ))}
          {candidates.data?.candidates?.length === 0 && (
            <p className="text-n500 text-sm">{t("wiki.noCandidates")}</p>
          )}
        </section>
        <section className="space-y-3">
          <h3 className="text-sm font-medium">{t("wiki.pages")}</h3>
          {(pages.data?.pages ?? []).map((page) => (
            <article key={page.id} className="border-hair space-y-2 rounded-lg border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-sm font-medium">{page.title}</h4>
                <MemoryStatus status={page.status} />
              </div>
              <p className="text-n500 text-xs">{t("revision", { revision: page.revision })}</p>
              {page.body_available ? (
                <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{page.body}</p>
              ) : (
                <p className="text-n500 text-sm">{t("wiki.staleHint")}</p>
              )}
              <details>
                <summary className="text-n600 cursor-pointer text-xs">{t("wiki.evidence")}</summary>
                <DiagnosticData
                  data={{
                    content_hash: page.content_hash,
                    sources: page.body_available ? page.sources : [],
                    paragraphs: page.body_available ? page.paragraphs : [],
                    reason_code: page.reason_code,
                  }}
                />
              </details>
            </article>
          ))}
          {pages.data?.pages?.length === 0 && <p className="text-n500 text-sm">{t("wiki.noPages")}</p>}
        </section>
      </div>
    </section>
  )
}
