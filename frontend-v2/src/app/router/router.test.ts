import { describe, expect, it } from "vitest"
import { matchRoutes } from "react-router"
import { router } from "./router"
import { paths } from "@/shared/router/paths"

/**
 * The console's URLs are handed around in ops runbooks and linked from the
 * account menu, so a typo in `routePatterns` would only surface as a 404 in the
 * browser. Matching against the real route table catches it here instead.
 *
 * `matchRoutes` returns the branch it would render; the last entry's
 * `route.path` is the pattern that actually claimed the URL.
 */
function claimedBy(pathname: string): string[] {
  const matches = matchRoutes(router.routes, pathname)
  expect(matches, `no route matches ${pathname}`).toBeTruthy()
  return matches!.map((m) => m.route.path ?? (m.route.index ? "(index)" : ""))
}

describe("admin console routes", () => {
  it.each([
    [paths.admin, "(index)"],
    [paths.adminFleet, "fleet"],
    [paths.adminSkills(), "skills/:tab?"],
    [paths.adminSkills("review"), "skills/:tab?"],
    [paths.adminSkills("installs"), "skills/:tab?"],
    [paths.adminBilling(), "billing/:tab?"],
    [paths.adminBilling("orders"), "billing/:tab?"],
    [paths.adminWorkspace("ws-1"), "billing/workspaces/:workspaceId"],
  ])("%s resolves to %s", (pathname, leaf) => {
    const branch = claimedBy(pathname)
    expect(branch).toContain("admin")
    expect(branch.at(-1)).toBe(leaf)
  })

  // `billing/workspaces/:id` and `billing/:tab?` both match this URL; React
  // Router ranks two static segments above one dynamic one, so the detail page
  // wins regardless of the order the children are declared in.
  it("prefers the workspace detail over the billing tab", () => {
    expect(claimedBy(paths.adminWorkspace("ws-1")).at(-1)).toBe(
      "billing/workspaces/:workspaceId",
    )
  })

  it("keeps the console behind /app so RequireAuth still wraps it", () => {
    expect(claimedBy(paths.adminFleet)).toContain(paths.app)
  })

  it("does not swallow a non-admin sibling of the console", () => {
    expect(claimedBy(paths.skills).at(-1)).toBe("skills")
  })
})
