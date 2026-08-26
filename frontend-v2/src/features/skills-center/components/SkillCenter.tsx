// 技能中心 — installed skills and MCP servers, and the store they come from.
//
// Skills and MCP servers sit in one place because they are one thing to the
// person using them: a capability the agent gains. Splitting them across two
// screens hides the dependency between them, which is exactly the relationship
// that breaks when it goes unnoticed.
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Plus, Search } from "lucide-react"
import {
  useAddMcpServer,
  useCatalog,
  useConnectMcpServer,
  useDisconnectMcpServer,
  useInstallFromCatalog,
  useInstallSkill,
  useInstalledSkills,
  useMcpServers,
  useRemoveMcpServer,
  useUninstallSkill,
  useUploadSkillArchive,
} from "@/features/skills-center/api/skills-center"
import type { CenterTab, InstalledSkill, KindFilter } from "@/features/skills-center/types"
import type { ParsedMcpEntry } from "@/features/skills-center/lib/parse-mcp-config"
import { useDependencyResolver } from "@/features/skills-center/hooks/useDependencies"
import {
  DependencyDialog,
  type Dependency,
  type DependencyTarget,
} from "./DependencyDialog"
import { InstallDialog, type InstallTarget } from "./InstallDialog"
import { MineList } from "./MineList"
import { StoreList } from "./StoreList"
import { UploadDialog } from "./UploadDialog"

const CENTER_TABS: CenterTab[] = ["mine", "store"]
const KIND_FILTERS: KindFilter[] = ["all", "skill", "mcp"]

function matches(query: string, ...fields: (string | undefined)[]): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return fields.some((f) => (f ?? "").toLowerCase().includes(q))
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export function SkillCenter() {
  const { t } = useTranslation("skills")
  const [tab, setTab] = useState<CenterTab>("mine")
  const [kind, setKind] = useState<KindFilter>("all")
  const [query, setQuery] = useState("")
  const [installTarget, setInstallTarget] = useState<InstallTarget | null>(null)
  const [depTarget, setDepTarget] = useState<DependencyTarget | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const skills = useInstalledSkills()
  const servers = useMcpServers()
  const catalog = useCatalog()

  const installFromCatalog = useInstallFromCatalog()
  const addMcp = useAddMcpServer()
  const removeMcp = useRemoveMcpServer()
  const connectMcp = useConnectMcpServer()
  const disconnectMcp = useDisconnectMcpServer()
  const uninstallSkill = useUninstallSkill()
  const installSkill = useInstallSkill()
  const uploadArchive = useUploadSkillArchive()

  const mcpServers = servers.data ?? []
  const unmetFor = useDependencyResolver(mcpServers, catalog.data?.mcp ?? [])

  // Every skill the agent can reach is listed, including the ones baked into
  // the sandbox image and the ones living on the backend. Hiding those made the
  // list an inventory of what is removable rather than of what the agent has,
  // so a capability like dev-browser was simply invisible. Removability is a
  // property of a row, not a reason to omit it.
  const mineSkills = useMemo(
    () => (skills.data ?? []).filter((s) => matches(query, s.name, s.description)),
    [skills.data, query],
  )
  const mineServers = useMemo(
    () => mcpServers.filter((s) => matches(query, s.name, s.url ?? s.command ?? "")),
    [mcpServers, query],
  )
  const storeSkills = useMemo(
    () =>
      (catalog.data?.skills ?? []).filter((s) =>
        matches(query, s.title, s.description, s.name, s.tags?.join(" ")),
      ),
    [catalog.data, query],
  )
  const storeMcp = useMemo(
    () =>
      (catalog.data?.mcp ?? []).filter((s) =>
        matches(query, s.title, s.description, s.name, s.tags?.join(" ")),
      ),
    [catalog.data, query],
  )

  const mutating =
    installFromCatalog.isPending ||
    addMcp.isPending ||
    installSkill.isPending ||
    uploadArchive.isPending
  const rowBusy =
    uninstallSkill.isPending ||
    removeMcp.isPending ||
    connectMcp.isPending ||
    disconnectMcp.isPending
  const loading = tab === "mine" ? skills.isLoading || servers.isLoading : catalog.isLoading

  function run<T>(promise: Promise<T>, onDone?: () => void) {
    setActionError(null)
    promise.then(
      () => onDone?.(),
      (e: unknown) => setActionError(errorText(e)),
    )
  }

  /**
   * Offer to close a freshly installed skill's gaps straight away.
   *
   * Left to the row warning alone, a skill installs "successfully" and then
   * quietly does not work until someone reads the warning and goes hunting for
   * the server themselves. Asked here, the install finishes usable.
   */
  function promptForDependencies(skill: Pick<InstalledSkill, "name" | "requires_mcp">) {
    const deps = unmetFor(skill)
    if (!deps.length) return false
    setDepTarget({ skillName: skill.name, deps })
    return true
  }

  /** Install what is missing, reconnect what is merely disconnected. */
  async function resolveDependencies(
    deps: Dependency[],
    env: Record<string, Record<string, string>>,
  ) {
    setActionError(null)
    try {
      for (const dep of deps) {
        if (dep.configured) {
          await connectMcp.mutateAsync(dep.name)
        } else if (dep.catalog) {
          await installFromCatalog.mutateAsync({
            id: dep.catalog.id,
            kind: "mcp",
            env: dep.catalog.required_env?.length ? { [dep.catalog.id]: env[dep.catalog.id] ?? {} } : {},
          })
        }
      }
      setDepTarget(null)
    } catch (e) {
      setActionError(errorText(e))
    }
  }

  /**
   * Finish a skill install by asking about what it still needs.
   *
   * The freshly installed skill is read back from the list rather than the
   * install response, because requires_mcp is parsed from the SKILL.md the
   * sandbox unpacked — the caller never had it.
   */
  async function finishSkillInstall(promise: Promise<unknown>) {
    setActionError(null)
    try {
      await promise
      setUploadOpen(false)
      const fresh = await skills.refetch()
      await servers.refetch()
      const installed = (fresh.data ?? []).filter((s) => (s.requires_mcp ?? []).length > 0)
      for (const skill of installed) {
        if (promptForDependencies(skill)) break
      }
    } catch (e) {
      setActionError(errorText(e))
    }
  }

  async function handleAddMcp(entries: ParsedMcpEntry[]) {
    // Sequential: one pasted config can carry several servers, and each stdio
    // server spawns a process on connect. Firing them together races for the
    // same npx cache.
    setActionError(null)
    try {
      for (const entry of entries) {
        await addMcp.mutateAsync({ name: entry.name, config: entry.config })
      }
      setUploadOpen(false)
    } catch (e) {
      setActionError(errorText(e))
    }
  }

  const showSkills = kind !== "mcp"
  const showMcp = kind !== "skill"

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex gap-1">
          {CENTER_TABS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded-full px-3.5 py-1.5 text-sm transition-colors ${
                tab === key ? "bg-ink text-bg" : "text-n700 hover:bg-hairsoft"
              }`}
            >
              {t(`tab.${key}`)}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => {
            setActionError(null)
            setUploadOpen(true)
          }}
          className="flex items-center gap-1.5 rounded-full border border-hair px-3 py-1.5 text-sm text-ink hover:bg-hairsoft"
        >
          <Plus size={15} />
          {t("action.add")}
        </button>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex flex-1 items-center gap-2 rounded-xl border border-hair bg-canvas px-3 py-2">
          <Search size={15} className="flex-none text-n600" aria-hidden />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("searchPlaceholder")}
            className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-n600"
          />
        </div>
        <div className="flex flex-none gap-1">
          {KIND_FILTERS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setKind(k)}
              className={`rounded-full px-2.5 py-1.5 text-xs transition-colors ${
                kind === k ? "bg-hairsoft text-ink" : "text-n600 hover:bg-hairsoft/60"
              }`}
            >
              {t(`filter.${k}`)}
            </button>
          ))}
        </div>
      </div>

      {actionError && !installTarget && !uploadOpen && (
        <p className="rounded-lg bg-dangersoft px-3 py-2 text-xs leading-5 text-danger">
          {actionError}
        </p>
      )}

      {loading ? (
        <div className="flex flex-col gap-1.5">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-hairsoft/50" />
          ))}
        </div>
      ) : tab === "mine" ? (
        <MineList
          skills={mineSkills}
          servers={mineServers}
          unmetFor={unmetFor}
          showSkills={showSkills}
          showMcp={showMcp}
          onBrowseStore={() => setTab("store")}
          actions={{
            busy: rowBusy,
            uninstallSkill: (dir, count) => {
              // A pack install unpacks into many skills that share one
              // directory, so removing it removes all of them. Say the number
              // rather than letting one click take eighteen others quietly.
              if (count > 1 && !window.confirm(t("mine.confirmPack", { name: dir, count }))) {
                return
              }
              run(uninstallSkill.mutateAsync(dir))
            },
            fixDependencies: (skill) => {
              setActionError(null)
              if (!promptForDependencies(skill)) {
                // Everything it needs is already connected — refresh so the
                // stale warning clears rather than sitting there.
                void servers.refetch()
              }
            },
            connect: (name) => run(connectMcp.mutateAsync(name)),
            disconnect: (name) => run(disconnectMcp.mutateAsync(name)),
            removeMcp: (name) => run(removeMcp.mutateAsync(name)),
          }}
        />
      ) : (
        <StoreList
          skills={storeSkills}
          mcp={storeMcp}
          showSkills={showSkills}
          showMcp={showMcp}
          onInstallSkill={(entry) => {
            setActionError(null)
            setInstallTarget({ kind: "skill", entry })
          }}
          onInstallMcp={(entry) => {
            setActionError(null)
            setInstallTarget({ kind: "mcp", entry })
          }}
        />
      )}

      {installTarget && (
        <InstallDialog
          target={installTarget}
          mcpCatalog={catalog.data?.mcp ?? []}
          busy={installFromCatalog.isPending}
          error={actionError}
          onCancel={() => {
            setInstallTarget(null)
            setActionError(null)
          }}
          onConfirm={(withMcp, env) =>
            run(
              installFromCatalog.mutateAsync({
                id: installTarget.entry.id,
                kind: installTarget.kind,
                withMcp,
                env,
              }),
              () => setInstallTarget(null),
            )
          }
        />
      )}

      {depTarget && (
        <DependencyDialog
          target={depTarget}
          busy={installFromCatalog.isPending || connectMcp.isPending}
          error={actionError}
          onCancel={() => {
            setDepTarget(null)
            setActionError(null)
          }}
          onConfirm={(deps, env) => void resolveDependencies(deps, env)}
        />
      )}

      {uploadOpen && (
        <UploadDialog
          busy={mutating}
          error={actionError}
          onCancel={() => {
            setUploadOpen(false)
            setActionError(null)
          }}
          onUploadArchive={(file, name) =>
            void finishSkillInstall(uploadArchive.mutateAsync({ file, name: name || undefined }))
          }
          onInstallSkill={(vars) => void finishSkillInstall(installSkill.mutateAsync(vars))}
          onAddMcp={(entries) => void handleAddMcp(entries)}
        />
      )}
    </div>
  )
}
