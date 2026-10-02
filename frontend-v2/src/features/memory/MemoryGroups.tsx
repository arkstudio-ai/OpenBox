import type { ReactNode } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"
import { http } from "@/shared/api/http"
import type { MemoryRecord } from "@/shared/api/memory"
import { paths } from "@/shared/router/paths"
import { useMemoryScope } from "./api"

interface Group {
  id: string
  title: string
  page_id: string | null
  memory_ids: string[]
}

export function MemoryGroups({
  memories,
  projectId,
  active,
  children,
}: {
  memories: MemoryRecord[]
  projectId: string
  active: boolean
  children: (memory: MemoryRecord) => ReactNode
}) {
  const { key } = useMemoryScope()
  const { data } = useQuery({
    queryKey: [...key, "memory-groups", projectId],
    queryFn: () =>
      http.get<{ groups: Group[] }>(
        "/api/memory-wiki/memory-groups" +
          (projectId ? "?" + new URLSearchParams({ project_id: projectId }) : ""),
      ),
    enabled: active,
    refetchInterval: 10000,
  })
  const byId = new Map(memories.map((memory) => [memory.id, memory]))
  const assigned = new Set<string>()
  const groups: { group: Group; rows: MemoryRecord[] }[] = []
  for (const group of active ? (data?.groups ?? []) : []) {
    const rows = group.memory_ids.flatMap((id) => (byId.has(id) && !assigned.has(id) ? [byId.get(id)!] : []))
    if (rows.length < 2) continue
    rows.forEach((row) => assigned.add(row.id))
    groups.push({ group, rows })
  }
  return (
    <>
      {groups.map(({ group, rows }) => (
        <section
          key={group.id}
          className="border-hair bg-rail/30 space-y-2 rounded-2xl border p-3"
          aria-label={group.title}
        >
          <h2 className="px-1 py-1 text-sm font-semibold">
            {group.page_id ? (
              <Link className="text-a700 hover:underline" to={paths.wikiPage(group.page_id, projectId)}>
                {group.title}
              </Link>
            ) : (
              group.title
            )}
          </h2>
          {rows.map(children)}
        </section>
      ))}
      {memories.filter((row) => !assigned.has(row.id)).map(children)}
    </>
  )
}
