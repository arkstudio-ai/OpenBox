import { paths } from "@/shared/router/paths"

/** The three columns of the admin console, in nav order. */
export const ADMIN_SECTIONS = ["fleet", "skills", "billing"] as const

export type AdminSection = (typeof ADMIN_SECTIONS)[number]

/**
 * Landing link per column. Skills and billing point at the tab-less path so the
 * URL stays short; their routes fall back to the first tab on their own.
 */
export const ADMIN_SECTION_PATHS: Record<AdminSection, string> = {
  fleet: paths.adminFleet,
  skills: paths.adminSkills(),
  billing: paths.adminBilling(),
}

/**
 * Which column owns a pathname. Matching on the first segment under
 * `/app/admin` keeps deep links (`billing/workspaces/:id`) highlighted on their
 * own column, and the `fleet` fallback agrees with the index redirect.
 */
export function activeAdminSection(pathname: string): AdminSection {
  const rest = pathname.startsWith(paths.admin) ? pathname.slice(paths.admin.length) : ""
  const segment = rest.split("/").filter(Boolean)[0]
  return ADMIN_SECTIONS.find((section) => section === segment) ?? "fleet"
}
