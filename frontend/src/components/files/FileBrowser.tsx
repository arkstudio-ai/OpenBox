import { useState, useEffect, useCallback } from "react"
import { useQuery } from "@tanstack/react-query"
import { Folder, FileText, ChevronRight, Search, ArrowUp, RefreshCw, Copy, X } from "lucide-react"
import { api } from "@/services/api"
import { Spinner } from "@/components/ui/Spinner"
import { formatBytes } from "@/lib/utils"

interface FileItem {
  name: string
  is_dir: boolean
  size: number | null
  modified: string | null
}

interface FileBrowserProps {
  containerId?: string
  sessionId: string
}

function cleanRoot(path: string): string {
  return path.length > 1 ? path.replace(/\/+$/, "") : path
}

function isInside(root: string, path: string): boolean {
  const base = cleanRoot(root)
  return path === base || path.startsWith(`${base}/`)
}

function relativePath(root: string, path: string): string | null {
  const base = cleanRoot(root)
  if (!isInside(base, path)) return null
  return path === base ? "." : path.slice(base.length + 1)
}

function parentPath(root: string, path: string): string {
  const base = cleanRoot(root)
  if (!isInside(base, path) || path === base) return base
  const parent = path.slice(0, path.lastIndexOf("/"))
  return isInside(base, parent) ? parent : base
}

export function FileBrowser({ containerId, sessionId }: FileBrowserProps) {
  const [currentPath, setCurrentPath] = useState("")
  const [openFile, setOpenFile] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState("")

  const { data: session } = useQuery({
    queryKey: ["session-workspace", sessionId],
    queryFn: () => api.getSession(sessionId),
    enabled: !!sessionId,
  })
  const root = session?.directory ? cleanRoot(session.directory) : ""
  const safePath = root && isInside(root, currentPath) ? currentPath : root

  // Fetch container details
  const { data: container } = useQuery({
    queryKey: ["container", containerId],
    queryFn: () => api.getContainer(containerId!),
    enabled: !!containerId,
    staleTime: 60000,
  })

  // Fetch files from API
  const { data: fileData, isLoading, refetch } = useQuery({
    queryKey: ["files", containerId, safePath],
    queryFn: () => api.listFiles(containerId!, safePath),
    enabled: !!containerId && !!safePath && !openFile,
  })

  const files: FileItem[] = fileData?.files ?? fileData?.entries ?? []

  const { data: opened, isLoading: fileLoading } = useQuery({
    queryKey: ["file-content", containerId, openFile],
    queryFn: () => api.getFileContent(containerId!, openFile!),
    enabled: !!containerId && !!openFile && !!root && isInside(root, openFile),
    retry: false,
  })

  const filteredFiles = searchQuery
    ? files.filter((f) => f.name.toLowerCase().includes(searchQuery.toLowerCase()))
    : files

  // A route/session switch must never retain the previous project's cwd.
  useEffect(() => {
    setCurrentPath(root)
    setOpenFile(null)
  }, [containerId, root, sessionId])

  const handleRefresh = useCallback(() => {
    refetch()
  }, [refetch])

  const pathParts = (relativePath(root, safePath) ?? "")
    .split("/")
    .filter((part) => part && part !== ".")
  const rootLabel = session?.project_name || "Project root"

  if (!containerId) {
    return (
      <div className="h-full flex items-center justify-center grid-pattern">
        <div className="text-center space-y-3">
          <div className="h-16 w-16 rounded-sm bg-[hsl(var(--accent))]/10 flex items-center justify-center mx-auto glow-amber">
            <Folder className="h-8 w-8 text-[hsl(var(--accent))]/40" />
          </div>
          <p className="text-sm font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))]">Select a sandbox to browse files</p>
        </div>
      </div>
    )
  }

  if (!root) {
    return (
      <div className="h-full flex items-center justify-center text-sm font-mono text-[hsl(var(--muted-foreground))]">
        Loading project files...
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 px-4 py-2.5 border-b border-[hsl(var(--border))]/50 bg-[hsl(var(--card))] text-sm">
        <button
          onClick={() => { setCurrentPath(root); setOpenFile(null) }}
          className="hover:text-[hsl(var(--primary))] transition-colors cursor-pointer font-mono text-xs"
        >
          {rootLabel}
        </button>
        {pathParts.map((part, i) => (
          <div key={i} className="flex items-center gap-1">
            <ChevronRight className="h-3 w-3 text-[hsl(var(--muted-foreground))]/50" />
            <button
              onClick={() => {
                setCurrentPath(`${root}/${pathParts.slice(0, i + 1).join("/")}`)
                setOpenFile(null)
              }}
              className="hover:text-[hsl(var(--primary))] transition-colors cursor-pointer font-mono text-xs"
            >
              {part}
            </button>
          </div>
        ))}
        <div className="ml-auto flex items-center gap-2.5">
          {container && (
            <span className="text-[10px] font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))]/50 px-2 py-0.5 rounded-sm border border-[hsl(var(--border))] mr-1">{container.name}</span>
          )}
          <button
            onClick={handleRefresh}
            className="p-1.5 rounded-sm hover:bg-[hsl(var(--muted))] transition-colors cursor-pointer"
            aria-label="Refresh files"
          >
            <RefreshCw className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
          </button>
          <button
            onClick={() => {
              if (openFile) {
                setOpenFile(null)
                return
              }
              setCurrentPath(parentPath(root, safePath))
            }}
            className="p-1.5 rounded-sm hover:bg-[hsl(var(--muted))] transition-colors cursor-pointer"
            aria-label="Go up"
          >
            <ArrowUp className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
          </button>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3 w-3 text-[hsl(var(--muted-foreground))]" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search..."
              className="pl-7 pr-2.5 py-1.5 text-xs font-mono rounded-sm border border-[hsl(var(--border))]/50 bg-[hsl(var(--surface-1))] w-40 focus:outline-none focus:ring-2 focus:ring-[hsl(var(--primary))]/20 focus:border-[hsl(var(--primary))]/30 transition-all"
            />
          </div>
        </div>
      </div>

      {/* File list / viewer. API paths stay absolute; every visible or copied
          path below is derived relative to this exact project root. */}
      {openFile ? (
        <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-[hsl(var(--border))]/50 px-4 py-2">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">
              {relativePath(root, openFile)}
            </span>
            <button
              onClick={() => {
                const path = relativePath(root, openFile)
                if (path) void navigator.clipboard.writeText(path)
              }}
              className="p-1.5 rounded-sm hover:bg-[hsl(var(--muted))]"
              aria-label="Copy project-relative path"
            >
              <Copy className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setOpenFile(null)}
              className="p-1.5 rounded-sm hover:bg-[hsl(var(--muted))]"
              aria-label="Close file"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <pre className="flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed">
            {fileLoading ? "Loading..." : opened?.content ?? "Unable to open this file."}
          </pre>
        </div>
      ) : (
      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-[hsl(var(--card))] z-10">
            <tr className="text-left text-[10px] font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))] border-b border-[hsl(var(--border))]/50">
              <th className="px-4 py-2.5">Name</th>
              <th className="px-4 py-2.5 w-24">Size</th>
              <th className="px-4 py-2.5 w-36">Modified</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={3} className="px-4 py-12 text-center">
                  <Spinner size="md" />
                </td>
              </tr>
            ) : filteredFiles.length === 0 ? (
              <tr>
                <td colSpan={3} className="px-4 py-12 text-center text-sm font-mono uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
                  {searchQuery ? "No matching files" : "No files found"}
                </td>
              </tr>
            ) : (
              filteredFiles.map((file) => (
                <tr
                  key={file.name}
                  onClick={() => {
                    const path = `${safePath}/${file.name}`
                    if (!isInside(root, path)) return
                    if (file.is_dir) setCurrentPath(path)
                    else setOpenFile(path)
                  }}
                  className="border-b border-[hsl(var(--border))]/30 hover:bg-[hsl(var(--muted))]/30 cursor-pointer transition-colors group"
                >
                  <td className="px-4 py-2.5 flex items-center gap-2.5">
                    {file.is_dir ? (
                      <div className="h-6 w-6 rounded-sm bg-[hsl(var(--accent))]/10 flex items-center justify-center">
                        <Folder className="h-3.5 w-3.5 text-[hsl(var(--accent))]" />
                      </div>
                    ) : (
                      <div className="h-6 w-6 rounded-sm bg-[hsl(var(--muted))]/50 flex items-center justify-center">
                        <FileText className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
                      </div>
                    )}
                    <span className={file.is_dir ? "font-mono font-medium group-hover:text-[hsl(var(--primary))] transition-colors" : "font-mono"}>{file.name}</span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-[hsl(var(--muted-foreground))] tabular-nums font-mono">
                    {file.size != null ? formatBytes(file.size) : "-"}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-[hsl(var(--muted-foreground))] font-mono">
                    {file.modified || "-"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      )}
    </div>
  )
}
