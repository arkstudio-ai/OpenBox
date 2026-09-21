import { useState } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { Users, MoreHorizontal, Play, Pencil, Plus } from "lucide-react"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { Dialog, DialogTitle, DialogActions } from "@/shared/ui/Dialog"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { Spinner } from "@/shared/ui/Spinner"
import { useDefinitions, useDefinitionWrite } from "../api/teams"
import type { AgentSpec, Definition, TeamSpec } from "../types"
import { BUTTON, PRIMARY, INPUT, Field } from "./FormFields"
import { VersionDialog } from "./VersionDialog"
import { AgentAvatar } from "./AgentAvatar"

const STATUSES = ["active", "draft", "archived"]

type Spec = AgentSpec | TeamSpec
type Kind = "agent" | "team"
const editPath = (kind: Kind, id: string) => (kind === "agent" ? paths.agentEditor(id) : paths.teamEditor(id))

function DefinitionSummary({ kind, definition }: { kind: Kind; definition: Definition<Spec> }) {
  const { t } = useTranslation("teams")
  const spec = definition.version.spec
  const agent = "instruction" in spec ? spec : null
  const team = "preset_members" in spec ? spec : null
  const sourceSession = definition.provenance?.session_id
  return (
    <div className="min-w-0 flex-1">
      <div className="flex flex-wrap items-center gap-2">
        <Link to={editPath(kind, definition.id)} className="truncate text-sm font-medium hover:underline">
          {definition.name}
        </Link>
        <span className="border-hair text-n600 rounded-full border px-2 py-0.5 text-xs">
          {t(`state.${definition.status}`)}
        </span>
        {definition.source !== "user" && (
          <span className="text-n600 text-xs">{t(`source.${definition.source}`)}</span>
        )}
        {Object.values(definition.version.capability_summary.tool_tiers ?? {}).includes("T2") && (
          <span className="text-a700 text-xs">{t("containsPaidTools")}</span>
        )}
        {typeof sourceSession === "string" && sourceSession && (
          <Link
            to={paths.chat(encodeURIComponent(sourceSession))}
            className="text-n600 text-xs hover:underline"
          >
            {t("sourceConversation")}
          </Link>
        )}
      </div>
      <p className="text-n600 mt-1 truncate text-xs">{agent?.when_to_use ?? team?.description}</p>
      <p className="text-n500 mt-1 truncate text-xs">
        {agent
          ? t("agentSummary", {
              model: definition.version.capability_summary.model ?? t("followDefault"),
              skills: agent.skill_refs.length,
              tools: agent.tool_allowlist.length,
            })
          : team?.preset_members.map((member) => member.alias).join(" · ") || t("autoTeam")}
      </p>
      {team && (
        <p className="text-n500 mt-1 text-xs">
          {[
            team.policy.member_selection === "coordinator_select" ? t("supplementBadge") : null,
            definition.run_count !== undefined ? t("runCount", { count: definition.run_count }) : null,
          ].filter(Boolean).join(" · ")}
        </p>
      )}
    </div>
  )
}

function DefinitionRow({
  kind,
  definition,
  onRun,
}: {
  kind: Kind
  definition: Definition<Spec>
  onRun: (id: string) => void
}) {
  const { t } = useTranslation("teams")
  const [menu, setMenu] = useState(false)
  const [dialog, setDialog] = useState<"duplicate" | "versions" | "archive" | null>(null)
  const [name, setName] = useState("")
  const write = useDefinitionWrite<Spec>(kind)
  const spec = definition.version.spec
  const previews = definition.member_previews?.slice(0, 3) ?? []
  return (
    <div className="bg-hairsoft flex items-center gap-3 rounded-xl p-3.5">
      {"instruction" in spec ? (
        <AgentAvatar display={spec.display} />
      ) : previews.length ? (
        <span className="isolate flex shrink-0 -space-x-4 rtl:space-x-reverse">
          {previews.map((member) => (
            <AgentAvatar key={member.alias} display={member.display} className="size-8 rounded-full" />
          ))}
        </span>
      ) : (
        <span className="border-hair bg-card flex size-10 flex-none items-center justify-center rounded-xl border">
          <Users className="size-5" />
        </span>
      )}
      <DefinitionSummary kind={kind} definition={definition} />
      {kind === "team" && definition.status === "active" && (
        <button
          type="button"
          title={t("run")}
          aria-label={t("run")}
          className="hover:bg-n200 rounded-full p-2"
          onClick={() => onRun(definition.id)}
        >
          <Play className="size-4" />
        </button>
      )}
      {!definition.readonly && definition.status !== "archived" && (
        <Link
          to={`${editPath(kind, definition.id)}${kind === "agent" ? "?try=1" : ""}`}
          title={t(kind === "agent" ? "try" : "edit")}
          aria-label={t(kind === "agent" ? "try" : "edit")}
          className="hover:bg-n200 rounded-full p-2"
        >
          {kind === "agent" ? <Play className="size-4" /> : <Pencil className="size-4" />}
        </Link>
      )}
      <div className="relative">
        <button
          type="button"
          onClick={() => setMenu(!menu)}
          aria-label={t("more")}
          className="hover:bg-n200 rounded-full p-2"
        >
          <MoreHorizontal className="size-4" />
        </button>
        <Menu open={menu} onClose={() => setMenu(false)} className="end-0 top-full min-w-36">
          <MenuItem
            onClick={() => {
              setName(t("copyName", { name: definition.name }))
              setDialog("duplicate")
              setMenu(false)
            }}
          >
            {t("duplicate")}
          </MenuItem>
          {!definition.readonly && (
            <MenuItem
              onClick={() => {
                setDialog("versions")
                setMenu(false)
              }}
            >
              {t("versions")}
            </MenuItem>
          )}
          {!definition.readonly && definition.status !== "archived" && (
            <MenuItem
              danger
              onClick={() => {
                setDialog("archive")
                setMenu(false)
              }}
            >
              {t("archive")}
            </MenuItem>
          )}
        </Menu>
      </div>
      {dialog === "versions" && (
        <VersionDialog kind={kind} definition={definition} onClose={() => setDialog(null)} />
      )}
      {dialog && dialog !== "versions" && (
        <Dialog open onClose={() => setDialog(null)} label={t(dialog)}>
          <DialogTitle>{t(dialog)}</DialogTitle>
          {dialog === "duplicate" ? (
            <Field label={t("name")} value={name} maxLength={40} onChange={setName} />
          ) : (
            <p className="text-n600 text-sm">{t("archiveHint")}</p>
          )}
          {write.error && <p className="text-danger text-xs">{write.error.message}</p>}
          <DialogActions>
            <button type="button" onClick={() => setDialog(null)} className={BUTTON}>
              {t("cancel")}
            </button>
            <button
              type="button"
              className={PRIMARY}
              disabled={write.isPending || (dialog === "duplicate" && !name.trim())}
              onClick={() =>
                write.mutate(
                  {
                    action: dialog,
                    id: definition.id,
                    name: dialog === "duplicate" ? name : undefined,
                    expected_revision: definition.revision,
                  },
                  { onSuccess: () => setDialog(null) },
                )
              }
            >
              {t(dialog)}
            </button>
          </DialogActions>
        </Dialog>
      )}
    </div>
  )
}

export function DefinitionList({
  kind,
  onRun,
  onChatCreate,
  onGenerate,
}: {
  kind: Kind
  onRun: (id: string) => void
  onChatCreate: () => void
  onGenerate: () => void
}) {
  const { t } = useTranslation("teams")
  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("")
  const [createMenu, setCreateMenu] = useState(false)
  const query = useDefinitions<Spec>(kind, search, status || undefined)
  const rows = query.data?.pages.flatMap((page) => page.items) ?? []
  const builtin = status && status !== "active" ? [] : (query.data?.pages[0]?.builtin ?? [])
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label={t("searchDefinitions")}
          placeholder={t("searchDefinitions")}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className={`${INPUT} min-w-36 flex-1`}
        />
        <select
          aria-label={t("filterStatus")}
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          className={cn(INPUT, "w-auto")}
        >
          <option value="">{t("allStates")}</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`state.${value}`)}
            </option>
          ))}
        </select>
        {kind === "agent" && (
          <button type="button" onClick={onChatCreate} className={PRIMARY}>
            {t("chatCreate")}
          </button>
        )}
        <div className="relative">
          <button
            type="button"
            onClick={() => setCreateMenu(!createMenu)}
            className={`${BUTTON} flex items-center gap-1`}
          >
            <Plus className="size-4" />
            {t("new")}
          </button>
          <Menu open={createMenu} onClose={() => setCreateMenu(false)} className="end-0 top-full min-w-40">
            <Link
              role="menuitem"
              to={editPath(kind, "new")}
              className="hover:bg-n200 rounded-full px-3 py-2 text-sm"
            >
              {t("manualCreate")}
            </Link>
            {kind === "agent" && (
              <MenuItem
                onClick={() => {
                  setCreateMenu(false)
                  onGenerate()
                }}
              >
                {t("generateDraft")}
              </MenuItem>
            )}
          </Menu>
        </div>
      </div>
      {query.isLoading && <Spinner className="size-5" />}
      {query.error && <p className="text-danger text-sm">{query.error.message}</p>}
      {!query.isLoading && !query.error && rows.length === 0 && (
        <div className="border-hair bg-card rounded-xl border p-5">
          <p className="font-medium">{t("emptyDefinitions")}</p>
          <p className="text-n600 mt-1 text-sm">
            {t(kind === "agent" ? "emptyAgentsHint" : "emptyTemplatesHint")}
          </p>
        </div>
      )}
      {rows.map((definition) => (
        <DefinitionRow key={definition.id} kind={kind} definition={definition} onRun={onRun} />
      ))}
      {query.hasNextPage && (
        <button type="button" onClick={() => void query.fetchNextPage()} className={BUTTON}>
          {t("loadMore")}
        </button>
      )}
      {builtin.length > 0 && (
        <>
          <h2 className="text-n600 pt-4 text-xs font-medium">{t("builtins")}</h2>
          {builtin.map((definition) => (
            <DefinitionRow key={definition.id} kind={kind} definition={definition} onRun={onRun} />
          ))}
        </>
      )}
    </div>
  )
}
