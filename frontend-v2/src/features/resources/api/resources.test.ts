import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { fetchResources } from "./resources"
import { resourceKeys } from "./keys"

afterEach(() => vi.restoreAllMocks())

it("sends description searches with the selected project, source and workspace", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue({ items: [], total: 0, hasMore: false })
  const query = { project: "project-2", source: "user" as const, kind: "all" as const, q: " 红色圆形 ", sort: "created" as const, limit: 200 }
  const signal = new AbortController().signal
  await fetchResources(query, "workspace-2", signal)
  const [path, options] = get.mock.calls[0]!
  const params = new URL(path, "http://localhost").searchParams
  expect(Object.fromEntries(params)).toEqual({ project: "project-2", source: "user", kind: "all", q: "红色圆形", sort: "created", limit: "200" })
  expect(options).toEqual({ signal, headers: { "X-Workspace-Id": "workspace-2" } })
  expect(resourceKeys.list("user", query, "workspace-1")).not.toEqual(resourceKeys.list("user", query, "workspace-2"))
})
