import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useMcpCapabilities } from "../api/teams"
import type { AgentSpec } from "../types"
import { BUTTON, INPUT } from "./FormFields"

type Ref = AgentSpec["mcp_refs"][number]
export function McpFields({
  value,
  onChange,
  requiredServers = [],
}: {
  value: Ref[]
  onChange: (value: Ref[]) => void
  requiredServers?: string[]
}) {
  const { t } = useTranslation("teams")
  const catalogue = useMcpCapabilities()
  const [name, setName] = useState("")
  const [drafts, setDrafts] = useState<Record<string, { source: string; text: string }>>({})
  const services = catalogue.data?.services ?? []
  const names = [...new Set([...services.map((service) => service.name), ...value.map((ref) => ref.server)])]
  const update = (server: string, tools: string[]) =>
    onChange(value.map((ref) => (ref.server === server ? { ...ref, tools } : ref)))
  return (
    <fieldset className="space-y-3">
      <legend className="mb-2 text-sm font-medium">{t("mcpServices")}</legend>
      <p className="text-n600 text-xs">{t("mcpExplicit")}</p>
      {catalogue.data?.enabled === false && <p className="text-a700 text-xs">{t("mcpDisabled")}</p>}
      {(catalogue.error || catalogue.data?.available === false) && (
        <p className="text-a700 text-xs">{t("mcpUnavailable")}</p>
      )}
      {!names.length && <p className="text-n600 text-xs">{t("mcpEmpty")}</p>}
      {names.map((server) => {
        const required = requiredServers.includes(server)
        const selected = value.find((ref) => ref.server === server)
        const live = services.find((entry) => entry.name === server)
        const candidates = [
          ...(live?.tools ?? []),
          ...(live?.resources.map((uri) => `resource:${uri}`) ?? []),
        ]
        return (
          <div key={server} className="border-hair space-y-2 rounded-lg border p-3">
            <label className="flex min-h-8 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={!!selected}
                disabled={required || catalogue.data?.enabled === false}
                onChange={(event) =>
                  onChange(
                    event.target.checked
                      ? [...value, { server, tools: ["*"] }]
                      : value.filter((ref) => ref.server !== server),
                  )
                }
              />
              <span className="min-w-0 break-all">{server}</span>
              {required && <span className="text-n600 text-xs">{t("requiredBySkill")}</span>}
              <span className="text-n600 ms-auto text-xs">
                {live ? t("mcpToolCount", { count: live.tools.length }) : t("mcpOffline")}
              </span>
            </label>
            {selected && (
              <>
                <label className="text-n600 block text-xs">
                  {t("mcpPatterns")}
                  <textarea
                    aria-label={t("mcpPatternsFor", { server })}
                    rows={2}
                    className={`${INPUT} mt-1.5`}
                    disabled={required}
                    value={
                      drafts[server]?.source === selected.tools.join("\n")
                        ? drafts[server].text
                        : selected.tools.join("\n")
                    }
                    onChange={(event) => {
                      const text = event.target.value
                      const tools = text
                        .split("\n")
                        .map((item) => item.trim())
                        .filter(Boolean)
                      setDrafts((current) => ({ ...current, [server]: { source: tools.join("\n"), text } }))
                      update(server, tools)
                    }}
                  />
                </label>
                <p className="text-n600 text-xs">{t("mcpPatternHint")}</p>
                {candidates.length > 0 && (
                  <details>
                    <summary className="text-n600 cursor-pointer text-xs">{t("mcpChooseTools")}</summary>
                    <div className="scr mt-2 max-h-40 space-y-1 overflow-auto">
                      {candidates.map((tool) => (
                        <label key={tool} className="flex min-h-9 items-center gap-2 text-xs">
                          <input
                            type="checkbox"
                            checked={selected.tools.includes("*") || selected.tools.includes(tool)}
                            disabled={required}
                            onChange={(event) =>
                              update(
                                server,
                                event.target.checked
                                  ? [...selected.tools.filter((item) => item !== "*"), tool]
                                  : (selected.tools.includes("*") ? candidates : selected.tools).filter(
                                      (item) => item !== tool,
                                    ),
                              )
                            }
                          />
                          <span className="break-all">{tool}</span>
                        </label>
                      ))}
                    </div>
                  </details>
                )}
              </>
            )}
          </div>
        )
      })}
      <div className="flex items-end gap-2">
        <label className="text-n600 min-w-0 flex-1 text-xs">
          {t("mcpAddName")}
          <input
            className={`${INPUT} mt-1.5`}
            value={name}
            maxLength={128}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <button
          type="button"
          className={BUTTON}
          disabled={
            !name.trim() ||
            names.includes(name.trim()) ||
            value.length >= 64 ||
            catalogue.data?.enabled === false
          }
          onClick={() => {
            onChange([...value, { server: name.trim(), tools: ["*"] }])
            setName("")
          }}
        >
          {t("mcpAdd")}
        </button>
      </div>
    </fieldset>
  )
}
