// Topic page editor: Markdown on the left, a live preview on the right.
// The preview uses react-markdown directly rather than the chat feature's
// Markdown (features never import each other, and a topic is not a stream).
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ApiError } from "@/shared/api/http"
import type { InboxLink, Topic, TopicInput } from "../api"
import { button, primary } from "../lib/styles"

const input = "border-hair bg-card min-h-10 w-full rounded-lg border px-3 text-sm"
const label = "mb-1 block text-sm font-medium"
const hint = "text-n600 mt-1 text-xs"

type CtaKind = "none" | "url" | "topic"

interface Draft {
  slug: string
  title: string
  coverUrl: string
  contentMd: string
  ctaLabel: string
  ctaKind: CtaKind
  ctaValue: string
}

function toDraft(source: Topic | null): Draft {
  const cta = source?.ctaLink
  return {
    slug: source?.slug ?? "",
    title: source?.title ?? "",
    coverUrl: source?.coverUrl ?? "",
    contentMd: source?.contentMd ?? "",
    ctaLabel: source?.ctaLabel ?? "",
    ctaKind: cta?.kind === "url" ? "url" : cta?.kind === "topic" ? "topic" : "none",
    ctaValue: cta?.kind === "url" ? cta.url : cta?.kind === "topic" ? cta.slug : "",
  }
}

function toTopicInput(draft: Draft): TopicInput | null {
  const slug = draft.slug.trim()
  const title = draft.title.trim()
  if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(slug) || !title) return null
  const coverUrl = draft.coverUrl.trim()
  if (coverUrl && !coverUrl.startsWith("https://")) return null
  const ctaLabel = draft.ctaLabel.trim()
  let ctaLink: InboxLink | null = null
  if (draft.ctaKind === "url") {
    const url = draft.ctaValue.trim()
    if (!url.startsWith("https://")) return null
    ctaLink = { kind: "url", url }
  } else if (draft.ctaKind === "topic") {
    const target = draft.ctaValue.trim()
    if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(target)) return null
    ctaLink = { kind: "topic", slug: target }
  }
  if (Boolean(ctaLabel) !== Boolean(ctaLink)) return null
  return {
    slug,
    title,
    coverUrl: coverUrl || null,
    contentMd: draft.contentMd,
    ctaLabel: ctaLabel || null,
    ctaLink,
  }
}

interface Props {
  source: Topic | null
  saving: boolean
  onCancel: () => void
  onSave: (body: TopicInput) => Promise<void>
}

export function TopicForm({ source, saving, onCancel, onSave }: Props) {
  const { t, i18n } = useTranslation(["admin-messages", "errors"])
  const [draft, setDraft] = useState<Draft>(() => toDraft(source))
  const [error, setError] = useState("")
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const slugLocked = source?.status === "published"

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const body = toTopicInput(draft)
    if (!body) {
      setError(t("form.invalid"))
      return
    }
    setError("")
    try {
      await onSave(body)
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "TOPIC_SLUG_TAKEN") setError(t("topics.slugTaken"))
        else if (i18n.exists(err.code, { ns: "errors" })) setError(t(err.code, { ns: "errors" }))
        else setError((err.meta as { message?: string }).message || err.message || t("common.requestFailed"))
      } else setError(t("common.requestFailed"))
    }
  }

  return (
    <form
      onSubmit={(e) => void submit(e)}
      className="border-hair bg-card flex flex-col gap-4 rounded-xl border p-4"
    >
      <h3 className="font-medium">{t(source ? "topics.editTitle" : "topics.newTitle")}</h3>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className={label} htmlFor="topic-slug">
            {t("topics.slug")}
          </label>
          <input
            id="topic-slug"
            className={`${input} font-mono`}
            value={draft.slug}
            disabled={slugLocked}
            onChange={(e) => set("slug", e.target.value)}
          />
          <p className={hint}>{t("topics.slugHint")}</p>
        </div>
        <div>
          <label className={label} htmlFor="topic-title">
            {t("topics.title")}
          </label>
          <input
            id="topic-title"
            className={input}
            maxLength={120}
            value={draft.title}
            onChange={(e) => set("title", e.target.value)}
          />
        </div>
        <div className="sm:col-span-2">
          <label className={label} htmlFor="topic-cover">
            {t("topics.cover")}
          </label>
          <input
            id="topic-cover"
            className={input}
            value={draft.coverUrl}
            onChange={(e) => set("coverUrl", e.target.value)}
          />
        </div>
        <div>
          <label className={label} htmlFor="topic-cta-label">
            {t("topics.ctaLabel")}
          </label>
          <input
            id="topic-cta-label"
            className={input}
            maxLength={40}
            value={draft.ctaLabel}
            onChange={(e) => set("ctaLabel", e.target.value)}
          />
          <p className={hint}>{t("topics.ctaHint")}</p>
        </div>
        <div>
          <label className={label} htmlFor="topic-cta-kind">
            {t("topics.ctaLink")}
          </label>
          <div className="flex gap-2">
            <select
              id="topic-cta-kind"
              className={`${input} w-40`}
              value={draft.ctaKind}
              onChange={(e) => set("ctaKind", e.target.value as CtaKind)}
            >
              <option value="none">{t("form.linkNone")}</option>
              <option value="url">{t("form.linkUrl")}</option>
              <option value="topic">{t("form.linkTopic")}</option>
            </select>
            {draft.ctaKind !== "none" && (
              <input
                aria-label={t(draft.ctaKind === "url" ? "form.url" : "form.topicSlug")}
                className={input}
                value={draft.ctaValue}
                onChange={(e) => set("ctaValue", e.target.value)}
              />
            )}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <label className={label} htmlFor="topic-content">
            {t("topics.content")}
          </label>
          <textarea
            id="topic-content"
            className={`${input} min-h-[24rem] py-2 font-mono`}
            value={draft.contentMd}
            onChange={(e) => set("contentMd", e.target.value)}
          />
        </div>
        <div>
          <p className={label}>{t("topics.preview")}</p>
          <div
            className="border-hair prose prose-sm max-w-none rounded-lg border p-4 text-sm"
            aria-label={t("topics.preview")}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.contentMd}</ReactMarkdown>
          </div>
        </div>
      </div>
      {error && (
        <p role="alert" className="text-danger text-sm">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" className={button} onClick={onCancel} disabled={saving}>
          {t("common.cancel")}
        </button>
        <button type="submit" className={primary} disabled={saving}>
          {t(saving ? "common.saving" : "common.save")}
        </button>
      </div>
    </form>
  )
}
