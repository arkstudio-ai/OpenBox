// 技能中心 — installed skills and MCP servers, and the store they come from.
//
// Skills and MCP servers sit in one place because they are one thing to the
// person using them: a capability the agent gains. Splitting them across two
// screens hides the dependency between them, which is exactly the relationship
// that breaks when it goes unnoticed.
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { paths } from "@/shared/router/paths"
import {
  useAddMcpServer,
  useCatalog,
  useConnectMcpServer,
  useCreateSkillChat,
  useDisconnectMcpServer,
  useDownloadSkillArchive,
  useInstallFromCatalog,
  useInstallSkill,
  useInstalledSkills,
  useMcpServers,
  usePublishSkill,
  useRemoveMcpServer,
  useSkillProjects,
  useUninstallSkill,
  useUploadSkillArchive,
} from "@/features/skills-center/api/skills-center"
import type { CenterTab, InstalledSkill, KindFilter } from "@/features/skills-center/types"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import type { ParsedMcpEntry } from "@/features/skills-center/lib/parse-mcp-config"
import { useDependencyResolver } from "@/features/skills-center/hooks/useDependencies"
import { DependencyDialog, type Dependency, type DependencyTarget } from "./DependencyDialog"
import { InstallDialog, type InstallTarget } from "./InstallDialog"
import { MineList } from "./MineList"
import { CreateSkillDialog } from "./CreateSkillDialog"
import { PublishSkillDialog } from "./PublishSkillDialog"
import { SkillCenterToolbar } from "./SkillCenterToolbar"
import { StoreList } from "./StoreList"
import { UploadDialog } from "./UploadDialog"

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
  const navigate = useNavigate()
  const [tab, setTab] = useState<CenterTab>("mine")
  const [kind, setKind] = useState<KindFilter>("all")
  const [query, setQuery] = useState("")
  const [installTarget, setInstallTarget] = useState<InstallTarget | null>(null)
  const [depTarget, setDepTarget] = useState<DependencyTarget | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [publishTarget, setPublishTarget] = useState<SkillGroup | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const skills = useInstalledSkills()
  const servers = useMcpServers()
  const catalog = useCatalog()
  const projects = useSkillProjects(createOpen)

  const installFromCatalog = useInstallFromCatalog()
  const addMcp = useAddMcpServer()
  const removeMcp = useRemoveMcpServer()
  const connectMcp = useConnectMcpServer()
  const disconnectMcp = useDisconnectMcpServer()
  const uninstallSkill = useUninstallSkill()
  const installSkill = useInstallSkill()
  const uploadArchive = useUploadSkillArchive()
  const publishSkill = usePublishSkill()
  const downloadSkill = useDownloadSkillArchive()
  const createSkillChat = useCreateSkillChat()

  const mcpServers = useMemo(() => servers.data ?? [], [servers.data])
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

  const mutating = [
    installFromCatalog.isPending,
    addMcp.isPending,
    installSkill.isPending,
    uploadArchive.isPending,
    createSkillChat.isPending,
  ].some(Boolean)
  const rowBusy = [
    uninstallSkill.isPending,
    removeMcp.isPending,
    connectMcp.isPending,
    disconnectMcp.isPending,
    publishSkill.isPending,
    downloadSkill.isPending,
  ].some(Boolean)
  const loading = tab === "mine" ? skills.isLoading || servers.isLoading : catalog.isLoading

  function run<T>(promise: Promise<T>, onDone?: (value: T) => void) {
    setActionError(null)
    promise.then(
      (value) => onDone?.(value),
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
  async function resolveDependencies(deps: Dependency[], env: Record<string, Record<string, string>>) {
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
      <SkillCenterToolbar
        filters={{ tab, kind, query }}
        onChange={{ tab: setTab, kind: setKind, query: setQuery }}
        onCreateChat={() => {
          setActionError(null)
          setCreateOpen(true)
        }}
        onAdd={() => {
          setActionError(null)
          setUploadOpen(true)
        }}
      />

      {actionError && !installTarget && !uploadOpen && !createOpen && !publishTarget && (
        <p className="bg-dangersoft text-danger rounded-lg px-3 py-2 text-xs leading-5">{actionError}</p>
      )}

      {loading ? (
        <div className="flex flex-col gap-1.5">
          {[0, 1, 2].map((i) => (
            <div key={i} className="bg-hairsoft/50 h-16 animate-pulse rounded-xl" />
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
            publishSkill: (group) => {
              setActionError(null)
              setPublishTarget(group)
            },
            downloadSkill: (dir) => run(downloadSkill.mutateAsync(dir)),
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

      {publishTarget ? (
        <PublishSkillDialog
          target={publishTarget}
          busy={publishSkill.isPending}
          error={actionError}
          onCancel={() => {
            setPublishTarget(null)
            setActionError(null)
          }}
          onConfirm={() => run(publishSkill.mutateAsync(publishTarget.id), () => setPublishTarget(null))}
        />
      ) : null}

      {createOpen ? (
        <CreateSkillDialog
          projects={projects.data ?? []}
          loading={projects.isLoading}
          busy={createSkillChat.isPending}
          error={actionError ?? (projects.error ? errorText(projects.error) : null)}
          onCancel={() => {
            setCreateOpen(false)
            setActionError(null)
          }}
          onConfirm={(projectId, brief) =>
            run(
              createSkillChat.mutateAsync({
                projectId,
                brief,
                prompt: t("create.prompt", { brief }),
              }),
              (session) => {
                setCreateOpen(false)
                navigate(paths.chat(session.id))
              },
            )
          }
        />
      ) : null}
    </div>
  )
}
