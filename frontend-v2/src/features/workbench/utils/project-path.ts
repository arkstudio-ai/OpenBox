export {
  projectScopedDisplayPath,
  projectScopedDisplayText,
  projectScopedToolText,
} from "@/shared/lib/project-path"

function cleanRoot(root: string): string {
  return root.length > 1 ? root.replace(/\/+$/, "") : root
}

function hasParentSegment(path: string): boolean {
  return path.split("/").some((segment) => segment === "..")
}

/** Resolve a stored relative/absolute file selection inside one project root. */
export function resolveProjectPath(openFile: string | null, root: string): string | null {
  if (!openFile || !root || openFile.includes("\0") || hasParentSegment(openFile)) return null
  const base = cleanRoot(root)
  if (openFile.startsWith("/")) {
    return openFile === base || openFile.startsWith(`${base}/`) ? openFile : null
  }
  const relative = openFile
    .split("/")
    .filter((segment) => segment && segment !== ".")
    .join("/")
  return relative ? `${base}/${relative}` : base
}

/** Public UI path: always relative to the selected project, preserving Unicode. */
export function projectRelativePath(path: string | null, root: string): string | null {
  if (!path || !root) return null
  const base = cleanRoot(root)
  if (path === base) return "."
  return path.startsWith(`${base}/`) ? path.slice(base.length + 1) : null
}

/** Parent directory clamped to the selected project root. */
export function projectParentPath(path: string, root: string): string {
  const base = cleanRoot(root)
  if (path === base || !path.startsWith(`${base}/`)) return base
  const cut = path.lastIndexOf("/")
  const parent = cut > 0 ? path.slice(0, cut) : base
  return parent === base || parent.startsWith(`${base}/`) ? parent : base
}
