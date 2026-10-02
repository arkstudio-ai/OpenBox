import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { afterEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { debugApi } from "./api"

afterEach(() => {
  vi.unstubAllGlobals()
  useAuthStore.getState().clearAuth()
  useWorkspaceStore.setState({ currentId: null })
})

it("submits the real HTTP request to a POST route declared by the backend replay router", async () => {
  // A permissive http.post mock hid the original run-specific 404. Derive
  // accepted paths from the server declarations and reject every other URL.
  const backend = readFileSync(resolve(process.cwd(), "../backend/api/memory_debug.py"), "utf8")
  const prefix = /APIRouter\s*\(\s*prefix\s*=\s*['"]([^'"]+)['"]/.exec(backend)?.[1]
  expect(prefix).toBeTruthy()
  const postPaths = new Set(
    [...backend.matchAll(/@router\.post\(\s*['"]([^'"]+)['"]/g)].map((match) => prefix + match[1]),
  )
  useAuthStore.setState({ accessToken: null, user: { id: "contract-user" } as never })
  useWorkspaceStore.setState({ currentId: "contract-workspace" })
  const requests: Request[] = []
  vi.stubGlobal("fetch", async (url: string, options: RequestInit) => {
    const request = new Request(new URL(url, "http://localhost:8081"), options)
    requests.push(request)
    if (request.method !== "POST" || !postPaths.has(new URL(request.url).pathname))
      return new Response(JSON.stringify({ detail: "Route not found" }), { status: 404 })
    return new Response(JSON.stringify({ run_id: "contract-run", attempt_id: "contract-attempt" }), {
      status: 200,
    })
  })
  const result = await debugApi.replay("contract-preview")
  expect(result.run_id).toBe("contract-run")
  expect(requests).toHaveLength(1)
  expect(new URL(requests[0].url).pathname).toBe("/api/memory-debug/replay")
  expect(requests[0].headers.get("X-Workspace-Id")).toBe("contract-workspace")
  expect(await requests[0].json()).toEqual({ preview_id: "contract-preview", confirm_cost: true })
})
