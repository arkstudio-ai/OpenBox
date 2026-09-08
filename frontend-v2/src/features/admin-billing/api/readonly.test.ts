// AC-12 as a test rather than a code review: 订阅管理 is read-only this round
// (§3-Q5), and "read-only" is a property of the whole data layer, not of any one
// component. A grep catches the case a reviewer would miss — a write hook added
// months from now that no page has wired up yet, so no rendering test sees it.
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { describe, expect, it } from "vitest"
import * as api from "./admin-billing"

// Vitest rewrites `import.meta.url` to a served URL, so the file is located
// from the project root instead — which is where vitest is rooted.
const source = readFileSync(
  resolve(process.cwd(), "src/features/admin-billing/api/admin-billing.ts"),
  "utf8",
)

describe("the admin billing data layer", () => {
  it("never mutates", () => {
    // Comments name these deliberately, so strip them before looking.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "")
    expect(code).not.toMatch(/useMutation/)
    expect(code).not.toMatch(/http\.(post|put|patch|delete)/)
  })

  it("exposes only queries", () => {
    const exported = Object.keys(api)
    expect(exported.length).toBeGreaterThan(0)
    for (const name of exported) expect(name).toMatch(/^use[A-Z]/)
  })
})
