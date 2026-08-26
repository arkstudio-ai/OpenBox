// The MCP half of the add dialog.
//
// Split out of UploadDialog so each stays readable, and because these two entry
// routes — fill in the pieces, or paste the snippet a README gave you — are the
// only part of adding a server that has any real shape to it.
import { useTranslation } from "react-i18next"

export type McpTab = "form" | "json"
export type Transport = "stdio" | "remote"

const FIELD =
  "mt-1 w-full rounded-lg border border-hair bg-canvas px-2.5 py-1.5 text-sm text-ink outline-none focus:border-accent"

const MCP_TABS: McpTab[] = ["form", "json"]
const TRANSPORTS: Transport[] = ["stdio", "remote"]

export interface McpFormState {
  tab: McpTab
  name: string
  transport: Transport
  command: string
  args: string
  envText: string
  url: string
  headersText: string
  json: string
  jsonError: string | null
}

export const emptyMcpForm: McpFormState = {
  tab: "form",
  name: "",
  transport: "stdio",
  // npx is what almost every published server's README starts with.
  command: "npx",
  args: "",
  envText: "",
  url: "",
  headersText: "",
  json: "",
  jsonError: null,
}

export function McpConfigForm({
  state,
  onChange,
}: {
  state: McpFormState
  onChange: (patch: Partial<McpFormState>) => void
}) {
  const { t } = useTranslation("skills")

  return (
    <>
      <div className="flex gap-1">
        {MCP_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => onChange({ tab })}
            className={`rounded-full px-2.5 py-1 text-xs ${
              state.tab === tab ? "bg-hairsoft text-ink" : "text-n600 hover:bg-hairsoft/60"
            }`}
          >
            {t(`upload.mcpTab.${tab}`)}
          </button>
        ))}
      </div>

      {state.tab === "form" ? (
        <>
          <label className="mt-3 block">
            <span className="text-xs text-n600">{t("upload.mcpName")}</span>
            <input
              value={state.name}
              onChange={(e) => onChange({ name: e.target.value })}
              placeholder={t("upload.namePlaceholder")}
              className={FIELD}
            />
          </label>

          <div className="mt-3 flex gap-1">
            {TRANSPORTS.map((tp) => (
              <button
                key={tp}
                type="button"
                onClick={() => onChange({ transport: tp })}
                className={`rounded-full px-2.5 py-1 text-xs ${
                  state.transport === tp ? "bg-ink text-bg" : "text-n700 hover:bg-hairsoft"
                }`}
              >
                {t(`upload.transport.${tp}`)}
              </button>
            ))}
          </div>

          {state.transport === "stdio" ? (
            <>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.command")}</span>
                <input
                  value={state.command}
                  onChange={(e) => onChange({ command: e.target.value })}
                  className={FIELD}
                />
              </label>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.args")}</span>
                <input
                  value={state.args}
                  onChange={(e) => onChange({ args: e.target.value })}
                  placeholder={t("upload.argsPlaceholder")}
                  className={FIELD}
                />
              </label>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.env")}</span>
                <textarea
                  value={state.envText}
                  onChange={(e) => onChange({ envText: e.target.value })}
                  rows={3}
                  placeholder={t("upload.envPlaceholder")}
                  className={`${FIELD} resize-none font-mono text-xs`}
                />
              </label>
            </>
          ) : (
            <>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.mcpUrl")}</span>
                <input
                  value={state.url}
                  onChange={(e) => onChange({ url: e.target.value })}
                  placeholder={t("upload.urlPlaceholder")}
                  className={FIELD}
                />
              </label>
              <label className="mt-3 block">
                <span className="text-xs text-n600">{t("upload.headers")}</span>
                <textarea
                  value={state.headersText}
                  onChange={(e) => onChange({ headersText: e.target.value })}
                  rows={3}
                  placeholder={t("upload.headersPlaceholder")}
                  className={`${FIELD} resize-none font-mono text-xs`}
                />
              </label>
              <p className="mt-1.5 text-xs leading-5 text-n600">{t("upload.remoteHint")}</p>
            </>
          )}
        </>
      ) : (
        <>
          <label className="mt-3 block">
            <span className="text-xs text-n600">{t("upload.jsonLabel")}</span>
            <textarea
              value={state.json}
              onChange={(e) => onChange({ json: e.target.value, jsonError: null })}
              rows={10}
              spellCheck={false}
              placeholder={t("upload.jsonPlaceholder")}
              className={`${FIELD} resize-none font-mono text-xs leading-5`}
            />
          </label>
          <p className="mt-1.5 text-xs leading-5 text-n600">{t("upload.jsonHint")}</p>
          {state.jsonError && <p className="mt-1.5 text-xs text-danger">{state.jsonError}</p>}
        </>
      )}
    </>
  )
}
