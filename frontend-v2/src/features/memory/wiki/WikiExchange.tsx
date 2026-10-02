import { useState } from "react"
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import { MemoryStatus, memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { platformApi, type ExchangeBundle, type ExchangeDocument } from "./platform-api"
import { Field, panel, saveDownload, ScopeHint, useWikiAction, WikiError } from "./platform-ui"

const exportFormats = ["okf", "json", "jsonld", "graphml", "marp", "llms"]

export function WikiExchange({ projectId }: { projectId: string }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const [format, setFormat] = useState("okf")
  const [file, setFile] = useState<File | null>(null)
  const [selected, setSelected] = useState("")
  const bundles = useInfiniteQuery({
    queryKey: [...key, "wiki-import-bundles", projectId],
    queryFn: ({ pageParam }) => platformApi.bundles(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
  const preview = useQuery({
    queryKey: [...key, "wiki-import-bundle", selected],
    queryFn: () => platformApi.bundle(selected),
    enabled: !!selected,
    refetchInterval: 10000,
  })
  const upload = useWikiAction(
    () => platformApi.importPreview(projectId, file!),
    (result) => setSelected(result.id),
  )
  const download = useWikiAction(() => platformApi.export(projectId, format), saveDownload)
  return (
    <section className="space-y-5" aria-label={t("exchange")}>
      <div className={panel}>
        <h2 className="text-xl font-semibold">{t("exchange")}</h2>
        <p className="text-n500 text-sm leading-6">{t("exchangeHint")}</p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t("exportFormat")}>
            <select
              className={memoryInput}
              value={format}
              onChange={(event) => setFormat(event.target.value)}
            >
              {exportFormats.map((value) => (
                <option key={value} value={value}>
                  {t("formats." + value)}
                </option>
              ))}
            </select>
          </Field>
          <button className={memoryPrimary} disabled={download.isPending} onClick={() => download.mutate()}>
            {t("downloadExport")}
          </button>
        </div>
        <WikiError error={download.error} />
      </div>
      <div className={panel}>
        <h3 className="font-semibold">{t("importOkf")}</h3>
        <ScopeHint projectId={projectId} />
        <p className="text-n500 text-sm">{t("importHint")}</p>
        <Field label={t("chooseArchive")}>
          <input
            type="file"
            accept=".zip,application/zip"
            className={memoryInput}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </Field>
        <button
          className={memoryPrimary}
          disabled={!file || file.size > 8 * 1024 * 1024 || upload.isPending}
          onClick={() => upload.mutate()}
        >
          {t("previewImport")}
        </button>
        <WikiError error={upload.error ?? bundles.error ?? preview.error} />
        {!!bundles.data?.pages[0]?.bundles.length && (
          <Field label={t("importHistory")}>
            <select
              className={memoryInput}
              value={selected}
              onChange={(event) => setSelected(event.target.value)}
            >
              <option value="">{t("choose")}</option>
              {bundles.data.pages
                .flatMap((page) => page.bundles)
                .map((bundle) => (
                  <option key={bundle.id} value={bundle.id}>
                    {new Date(bundle.created_at).toLocaleString()} · {bundle.id.slice(-6)}
                  </option>
                ))}
            </select>
          </Field>
        )}
        {bundles.hasNextPage && (
          <button className={memoryButton} onClick={() => void bundles.fetchNextPage()}>
            {t("loadMore")}
          </button>
        )}
      </div>
      {preview.data && !preview.error && <ImportReview bundle={preview.data} />}
    </section>
  )
}

function ImportReview({ bundle }: { bundle: ExchangeBundle }) {
  const { t } = useTranslation("wiki")
  const [selected, setSelected] = useState("")
  const document = bundle.documents.find((item) => item.id === selected) ?? bundle.documents[0]
  return (
    <div className={panel}>
      <h3 className="font-semibold">{t("importReview")}</h3>
      <p className="text-n500 text-sm">{t("foreignAuthorityHint")}</p>
      {!!bundle.warnings.length && (
        <details open className="text-danger text-sm">
          <summary>{t("importWarnings", { count: bundle.warnings.length })}</summary>
          <ul className="mt-3 list-disc ps-5">
            {bundle.warnings.map((warning, index) => (
              <li key={index}>
                {warning.path} · {t("importWarningCodes." + warning.code)} {warning.target}
              </li>
            ))}
          </ul>
        </details>
      )}
      <Field label={t("importDocument")}>
        <select
          className={memoryInput}
          value={document?.id ?? ""}
          onChange={(event) => setSelected(event.target.value)}
        >
          {bundle.documents.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title ?? t("stale")} · {item.path}
            </option>
          ))}
        </select>
      </Field>
      {document && (
        <DocumentReview key={document.id + document.revision} document={document} bundle={bundle} />
      )}
    </div>
  )
}

function DocumentReview({ document, bundle }: { document: ExchangeDocument; bundle: ExchangeBundle }) {
  const { t } = useTranslation("wiki")
  const [slug, setSlug] = useState(document.slug)
  const [acknowledged, setAcknowledged] = useState(false)
  const decision = useWikiAction((action: "approve" | "reject" | "rename") =>
    platformApi.importDecision(document, action, {
      ...(action === "rename" ? { slug } : {}),
      acknowledge_warnings: acknowledged,
    }),
  )
  return (
    <article className="space-y-4">
      <div className="flex items-center gap-3">
        <h4 className="font-semibold">{document.title ?? t("stale")}</h4>
        <MemoryStatus status={document.status} />
      </div>
      {document.body && (
        <pre className="bg-rail max-h-96 overflow-auto rounded-lg p-4 text-sm leading-6 break-words whitespace-pre-wrap">
          {document.body}
        </pre>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer">{t("foreignMetadata")}</summary>
        <pre className="max-h-60 overflow-auto break-words whitespace-pre-wrap">
          {JSON.stringify(document.frontmatter, null, 2)}
        </pre>
      </details>
      {document.conflict && (
        <p role="status" className="text-danger text-sm">
          {t("importConflict")}
        </p>
      )}
      <WikiError error={decision.error} />
      {document.status === "pending" && (
        <>
          <div className="flex flex-wrap items-end gap-2">
            <Field label={t("pageAddress")}>
              <input
                className={memoryInput}
                value={slug}
                pattern="[a-z0-9][a-z0-9_-]{0,79}"
                maxLength={80}
                onChange={(event) => setSlug(event.target.value)}
              />
            </Field>
            <button
              className={memoryButton}
              disabled={slug === document.slug || decision.isPending}
              onClick={() => decision.mutate("rename")}
            >
              {t("renameImport")}
            </button>
          </div>
          {!!bundle.warnings.length && (
            <label className="flex gap-2 text-sm">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              {t("acknowledgeImportWarnings")}
            </label>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              className={memoryPrimary}
              disabled={
                document.conflict || (!!bundle.warnings.length && !acknowledged) || decision.isPending
              }
              onClick={() => decision.mutate("approve")}
            >
              {t("approve")}
            </button>
            <button
              className={memoryButton}
              disabled={decision.isPending}
              onClick={() => decision.mutate("reject")}
            >
              {t("reject")}
            </button>
          </div>
        </>
      )}
      {document.status === "approved" && document.page_id && (
        <Link className={memoryButton} to={paths.wikiPage(document.page_id, bundle.project_id ?? "")}>
          {t("readPage")}
        </Link>
      )}
    </article>
  )
}
