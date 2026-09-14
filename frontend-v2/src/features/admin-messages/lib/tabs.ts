// The two columns of 消息通知. The route reads `:tab` and falls back to the
// first entry, so the order here is also the default.
export const ADMIN_MESSAGE_TABS = ["announcements", "topics"] as const

export type AdminMessageTab = (typeof ADMIN_MESSAGE_TABS)[number]

export function isAdminMessageTab(value: string | undefined): value is AdminMessageTab {
  return ADMIN_MESSAGE_TABS.some((tab) => tab === value)
}
