// Draft editor for one announcement: copy, where it opens, who gets it, push,
// schedule and expiry. Validation mirrors the server (docs/MESSAGE_CENTER.md);
// the server's 422 text is surfaced verbatim when the two disagree.
import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import type { Announcement, AnnouncementInput, Audience, InboxLink } from "../api"
import { button, primary } from "../lib/styles"

const input = "border-hair bg-card min-h-10 w-full rounded-lg border px-3 text-sm"
const label = "mb-1 block text-sm font-medium"
const hint = "text-n600 mt-1 text-xs"

type LinkKind = "none" | "topic" | "url"
type AudienceKind = Audience["kind"]

interface Draft {
  title: string
  body: string
  linkKind: LinkKind
  slug: string
  url: string
  audienceKind: AudienceKind
  role: "admin" | "user"
  workspaceId: string
  userIds: string
  push: boolean
  publishAt: string
  expiresAt: string
}

/** ISO → value for `<input type="datetime-local">` in the browser's zone. */
function toLocalInput(iso: string | null): string {
  if (!iso) return ""
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fromLocalInput(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

function draftLink(link: InboxLink | null | undefined): Pick<Draft, "linkKind" | "slug" | "url"> {
  if (link?.kind === "topic") return { linkKind: "topic", slug: link.slug, url: "" }
  if (link?.kind === "url") return { linkKind: "url", slug: "", url: link.url }
  return { linkKind: "none", slug: "", url: "" }
}

function draftAudience(
  audience: Audience | undefined,
): Pick<Draft, "audienceKind" | "role" | "workspaceId" | "userIds"> {
  const base = { audienceKind: audience?.kind ?? "all", role: "user" as const, workspaceId: "", userIds: "" }
  switch (audience?.kind) {
    case "role":
      return { ...base, role: audience.role }
    case "workspace":
      return { ...base, workspaceId: audience.id }
    case "users":
      return { ...base, userIds: audience.ids.join("\n") }
    default:
      return base
  }
}

function toDraft(source: Announcement | null): Draft {
  return {
    title: source?.title ?? "",
    body: source?.body ?? "",
    ...draftLink(source?.link),
    ...draftAudience(source?.audience),
    push: source?.push ?? false,
    publishAt: toLocalInput(source?.publishAt ?? null),
    expiresAt: toLocalInput(source?.expiresAt ?? null),
  }
}

function toInput(draft: Draft): AnnouncementInput | null {
  const title = draft.title.trim()
  if (!title) return null
  let link: InboxLink | null = null
  if (draft.linkKind === "topic") {
    const slug = draft.slug.trim()
    if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(slug)) return null
    link = { kind: "topic", slug }
  } else if (draft.linkKind === "url") {
    const url = draft.url.trim()
    if (!url.startsWith("https://")) return null
    link = { kind: "url", url }
  }
  let audience: Audience
  switch (draft.audienceKind) {
    case "role":
      audience = { kind: "role", role: draft.role }
      break
    case "workspace": {
      const id = draft.workspaceId.trim()
      if (!id) return null
      audience = { kind: "workspace", id }
      break
    }
    case "users": {
      const ids = [
        ...new Set(
          draft.userIds
            .split(/[\s,]+/)
            .map((v) => v.trim())
            .filter(Boolean),
        ),
      ]
      if (ids.length === 0 || ids.length > 5000) return null
      audience = { kind: "users", ids }
      break
    }
    default:
      audience = { kind: "all" }
  }
  const publishAt = fromLocalInput(draft.publishAt)
  const expiresAt = fromLocalInput(draft.expiresAt)
  if (expiresAt && Date.parse(expiresAt) <= Date.now()) return null
  if (expiresAt && publishAt && Date.parse(expiresAt) <= Date.parse(publishAt)) return null
  return { title, body: draft.body.trim(), link, audience, push: draft.push, publishAt, expiresAt }
}

interface Props {
  open: boolean
  source: Announcement | null
  saving: boolean
  onClose: () => void
  onSave: (body: AnnouncementInput) => Promise<void>
}

export function AnnouncementForm({ open, source, saving, onClose, onSave }: Props) {
  const { t, i18n } = useTranslation(["admin-messages", "errors"])
  const [draft, setDraft] = useState<Draft>(() => toDraft(source))
  const [error, setError] = useState("")
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const body = toInput(draft)
    if (!body) {
      setError(t("form.invalid"))
      return
    }
    setError("")
    try {
      await onSave(body)
    } catch (err) {
      if (err instanceof ApiError) {
        const detail = (err.meta as { message?: string }).message
        setError(
          i18n.exists(err.code, { ns: "errors" })
            ? t(err.code, { ns: "errors" })
            : detail || err.message || t("common.requestFailed"),
        )
      } else setError(t("common.requestFailed"))
    }
  }

  return (
    <Dialog open={open} onClose={onClose} wide label={t(source ? "form.editTitle" : "form.newTitle")}>
      <DialogTitle>{t(source ? "form.editTitle" : "form.newTitle")}</DialogTitle>
      <form onSubmit={(e) => void submit(e)} className="contents">
        <DialogBody>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label className={label} htmlFor="ann-title">
                {t("form.title")}
              </label>
              <input
                id="ann-title"
                className={input}
                maxLength={120}
                value={draft.title}
                onChange={(e) => set("title", e.target.value)}
              />
            </div>
            <div className="sm:col-span-2">
              <label className={label} htmlFor="ann-body">
                {t("form.body")}
              </label>
              <textarea
                id="ann-body"
                className={`${input} min-h-20 py-2`}
                maxLength={500}
                value={draft.body}
                onChange={(e) => set("body", e.target.value)}
              />
              <p className={hint}>{t("form.bodyHint")}</p>
            </div>
            <div>
              <label className={label} htmlFor="ann-link">
                {t("form.link")}
              </label>
              <select
                id="ann-link"
                className={input}
                value={draft.linkKind}
                onChange={(e) => set("linkKind", e.target.value as LinkKind)}
              >
                <option value="none">{t("form.linkNone")}</option>
                <option value="topic">{t("form.linkTopic")}</option>
                <option value="url">{t("form.linkUrl")}</option>
              </select>
            </div>
            {draft.linkKind === "topic" && (
              <div>
                <label className={label} htmlFor="ann-slug">
                  {t("form.topicSlug")}
                </label>
                <input
                  id="ann-slug"
                  className={input}
                  value={draft.slug}
                  onChange={(e) => set("slug", e.target.value)}
                />
              </div>
            )}
            {draft.linkKind === "url" && (
              <div>
                <label className={label} htmlFor="ann-url">
                  {t("form.url")}
                </label>
                <input
                  id="ann-url"
                  className={input}
                  value={draft.url}
                  onChange={(e) => set("url", e.target.value)}
                />
              </div>
            )}
            <div>
              <label className={label} htmlFor="ann-audience">
                {t("form.audience")}
              </label>
              <select
                id="ann-audience"
                className={input}
                value={draft.audienceKind}
                onChange={(e) => set("audienceKind", e.target.value as AudienceKind)}
              >
                <option value="all">{t("form.audienceAll")}</option>
                <option value="role">{t("form.audienceRole")}</option>
                <option value="workspace">{t("form.audienceWorkspace")}</option>
                <option value="users">{t("form.audienceUsers")}</option>
              </select>
            </div>
            {draft.audienceKind === "role" && (
              <div>
                <label className={label} htmlFor="ann-role">
                  {t("form.role")}
                </label>
                <select
                  id="ann-role"
                  className={input}
                  value={draft.role}
                  onChange={(e) => set("role", e.target.value as "admin" | "user")}
                >
                  <option value="user">{t("form.roleUser")}</option>
                  <option value="admin">{t("form.roleAdmin")}</option>
                </select>
              </div>
            )}
            {draft.audienceKind === "workspace" && (
              <div>
                <label className={label} htmlFor="ann-workspace">
                  {t("form.workspaceId")}
                </label>
                <input
                  id="ann-workspace"
                  className={input}
                  value={draft.workspaceId}
                  onChange={(e) => set("workspaceId", e.target.value)}
                />
              </div>
            )}
            {draft.audienceKind === "users" && (
              <div className="sm:col-span-2">
                <label className={label} htmlFor="ann-users">
                  {t("form.userIds")}
                </label>
                <textarea
                  id="ann-users"
                  className={`${input} min-h-20 py-2 font-mono`}
                  value={draft.userIds}
                  onChange={(e) => set("userIds", e.target.value)}
                />
                <p className={hint}>{t("form.userIdsHint")}</p>
              </div>
            )}
            <div>
              <label className={label} htmlFor="ann-publish-at">
                {t("form.publishAt")}
              </label>
              <input
                id="ann-publish-at"
                type="datetime-local"
                className={input}
                value={draft.publishAt}
                onChange={(e) => set("publishAt", e.target.value)}
              />
              <p className={hint}>{t("form.publishAtHint")}</p>
            </div>
            <div>
              <label className={label} htmlFor="ann-expires-at">
                {t("form.expiresAt")}
              </label>
              <input
                id="ann-expires-at"
                type="datetime-local"
                className={input}
                value={draft.expiresAt}
                onChange={(e) => set("expiresAt", e.target.value)}
              />
              <p className={hint}>{t("form.expiresAtHint")}</p>
            </div>
            <div className="sm:col-span-2">
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={draft.push}
                  onChange={(e) => set("push", e.target.checked)}
                />
                <span>
                  {t("form.push")}
                  <span className={`${hint} block`}>{t("form.pushHint")}</span>
                </span>
              </label>
            </div>
          </div>
          {error && (
            <p role="alert" className="text-danger mt-3 text-sm">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogActions>
          <button type="button" className={button} onClick={onClose} disabled={saving}>
            {t("common.cancel")}
          </button>
          <button type="submit" className={primary} disabled={saving}>
            {t(saving ? "common.saving" : "common.save")}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
