import { paths } from "@/shared/router/paths"

/** The sections of the admin console, in nav order. */
export const ADMIN_SECTIONS = ["fleet", "skills", "billing", "trajectories", "notifications"] as const

export type AdminSection = (typeof ADMIN_SECTIONS)[number]

/**
 * Landing link per column. Skills and billing point at the tab-less path so the
 * URL stays short; their routes fall back to the first tab on their own.
 */
export const ADMIN_SECTION_PATHS: Record<AdminSection, string> = {
  fleet: paths.adminFleet,
  notifications: paths.adminNotifications,
  skills: paths.adminSkills(),
  billing: paths.adminBilling(),
  trajectories: paths.adminTrajectories(),
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

export interface AdminLayout {
  /** Drop the reading-width cap: the trajectory table and inspector need the screen. */
  wide: boolean
  /** Show the column heading. A session detail has its own identity header instead. */
  heading: boolean
}

export function adminLayout(pathname: string): AdminLayout {
  if (activeAdminSection(pathname) !== "trajectories") return { wide: false, heading: true }
  const isDetail = pathname.startsWith(`${paths.adminTrajectories()}/sessions/`)
  return { wide: true, heading: !isDetail }
}
