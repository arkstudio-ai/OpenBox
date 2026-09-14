// Public surface of 消息通知. The route reads `:tab`, picks a page, and renders
// the column's own title/subtitle from the `admin-messages` namespace.
export { AnnouncementsPage } from "./components/AnnouncementsPage"
export { TopicsPage } from "./components/TopicsPage"
export { ADMIN_MESSAGE_TABS, isAdminMessageTab, type AdminMessageTab } from "./lib/tabs"
