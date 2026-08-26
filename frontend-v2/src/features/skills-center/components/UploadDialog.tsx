// Adding something the store does not carry.
//
// Four routes in, because that is how skills and servers actually arrive:
// an archive someone exported, a SKILL.md pasted from an editor, a git repo,
// and — for MCP — the JSON snippet every server's README hands out.
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { parseMcpConfig, type ParsedMcpEntry } from "@/features/skills-center/lib/parse-mcp-config"
import type { McpConfig } from "@/features/skills-center/types"
import { McpConfigForm, emptyMcpForm, type McpFormState } from "./McpConfigForm"

type Mode = "archive" | "paste" | "git" | "mcp"

const MODES: Mode[] = ["archive", "paste", "git", "mcp"]

const FIELD =
  "mt-1 w-full rounded-lg border border-hair bg-canvas px-2.5 py-1.5 text-sm text-ink outline-none focus:border-accent"

/** "KEY=value" or "Key: value" per line — the shape people already have. */
function parsePairs(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split("\n")) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith("#")) continue
    const eq = trimmed.indexOf("=")
    const colon = trimmed.indexOf(":")
    const at = eq === -1 ? colon : colon === -1 ? eq : Math.min(eq, colon)
    if (at <= 0) continue
    out[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim()
  }
  return out
}

function buildMcpConfig(form: McpFormState): McpConfig | null {
  if (form.transport === "stdio") {
    const command = form.command.trim()
    if (!command) return null
    return {
      type: "stdio",
      command,
      // Whitespace-split: MCP commands are flat argv lists in practice.
      args: form.args.trim() ? form.args.trim().split(/\s+/) : [],
      env: parsePairs(form.envText),
      timeout: 60,
    }
  }
  const url = form.url.trim()
  if (!url) return null
  return { type: "remote", url, headers: parsePairs(form.headersText), timeout: 60 }
}

export function UploadDialog({
  busy,
  error,
  onCancel,
  onUploadArchive,
  onInstallSkill,
  onAddMcp,
}: {
  busy: boolean
  error?: string | null
  onCancel: () => void
  onUploadArchive: (file: File, name: string) => void
  onInstallSkill: (vars: { url?: string; name?: string; content?: string }) => void
  onAddMcp: (entries: ParsedMcpEntry[]) => void
}) {
  const { t } = useTranslation("skills")
  const [mode, setMode] = useState<Mode>("archive")

  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState("")
  const [content, setContent] = useState("")
  const [url, setUrl] = useState("")
  const [mcp, setMcp] = useState<McpFormState>(emptyMcpForm)

  function patchMcp(patch: Partial<McpFormState>) {
    setMcp((prev) => ({ ...prev, ...patch }))
  }

  function submit() {
    if (mode === "archive") {
      if (file) onUploadArchive(file, name.trim())
      return
    }
    if (mode === "paste") {
      if (content.trim()) onInstallSkill({ content, name: name.trim() || undefined })
      return
    }
    if (mode === "git") {
      if (url.trim()) onInstallSkill({ url: url.trim(), name: name.trim() || undefined })
      return
    }

    if (mcp.tab === "json") {
      const result = parseMcpConfig(mcp.json, mcp.name)
      if (result.error || !result.entries.length) {
        patchMcp({ jsonError: t(`upload.jsonError.${result.error ?? "noServers"}`) })
        return
      }
      onAddMcp(result.entries)
      return
    }

    const serverName = mcp.name.trim()
    const config = buildMcpConfig(mcp)
    if (!serverName || !config) return
    onAddMcp([{ name: serverName, config }])
  }

  const canSubmit =
    mode === "archive"
      ? Boolean(file)
      : mode === "paste"
        ? Boolean(content.trim())
        : mode === "git"
          ? Boolean(url.trim())
          : mcp.tab === "json"
            ? Boolean(mcp.json.trim())
            : Boolean(mcp.name.trim()) &&
              (mcp.transport === "stdio" ? Boolean(mcp.command.trim()) : Boolean(mcp.url.trim()))

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("upload.title")}
    >
      <div className="flex max-h-[86vh] w-full max-w-[520px] flex-col overflow-hidden rounded-2xl border border-hair bg-card shadow-xl">
        <div className="px-5 pb-3 pt-5">
          <h2 className="text-base font-medium text-ink">{t("upload.title")}</h2>
          <p className="mt-0.5 text-xs leading-5 text-n600">{t("upload.subtitle")}</p>
        </div>

        <div className="flex gap-1 px-5">
          {MODES.map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`rounded-full px-3 py-1 text-xs transition-colors ${
                mode === m ? "bg-ink text-bg" : "text-n700 hover:bg-hairsoft"
              }`}
            >
              {t(`upload.mode${m.charAt(0).toUpperCase()}${m.slice(1)}`)}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-3">
          {mode === "archive" && (
            <>
              <input
                ref={fileRef}
                type="file"
                accept=".zip,.tar,.tar.gz,.tgz"
                className="hidden"
                onChange={(e) => {
                  const picked = e.target.files?.[0] ?? null
                  setFile(picked)
                  if (picked && !name) setName(picked.name.replace(/\.(zip|tgz|tar|tar\.gz)$/i, ""))
                }}
              />
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                className="flex w-full flex-col items-center gap-1 rounded-xl border border-dashed border-hair bg-hairsoft/40 px-4 py-7 text-center hover:bg-hairsoft"
              >
                <span className="text-sm text-ink">
                  {file ? file.name : t("upload.pickArchive")}
                </span>
                <span className="text-xs text-n600">{t("upload.archiveHint")}</span>
              </button>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.nameLabel")}</span>
                <input value={name} onChange={(e) => setName(e.target.value)} className={FIELD} />
              </label>
            </>
          )}

          {mode === "paste" && (
            <>
              <label className="block">
                <span className="text-xs text-n600">{t("upload.contentLabel")}</span>
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  rows={11}
                  spellCheck={false}
                  placeholder={t("upload.skillTemplate")}
                  className={`${FIELD} resize-none font-mono text-xs leading-5`}
                />
              </label>
              <p className="mt-1.5 text-xs leading-5 text-n600">{t("upload.frontmatterHint")}</p>
            </>
          )}

          {mode === "git" && (
            <>
              <label className="block">
                <span className="text-xs text-n600">{t("upload.gitLabel")}</span>
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder={t("upload.gitPlaceholder")}
                  className={FIELD}
                />
              </label>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.nameLabel")}</span>
                <input value={name} onChange={(e) => setName(e.target.value)} className={FIELD} />
              </label>
              <p className="mt-1.5 text-xs leading-5 text-n600">{t("upload.gitHint")}</p>
            </>
          )}

          {mode === "mcp" && <McpConfigForm state={mcp} onChange={patchMcp} />}

          {error && (
            <p className="mt-3 rounded-lg bg-dangersoft px-3 py-2 text-xs leading-5 text-danger">
              {error}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 pb-5 pt-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-full px-3.5 py-1.5 text-sm text-n700 hover:bg-hairsoft disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy || !canSubmit}
            className="rounded-full bg-ink px-3.5 py-1.5 text-sm text-bg hover:opacity-90 disabled:opacity-50"
          >
            {busy ? t("upload.installing") : t("upload.confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}
