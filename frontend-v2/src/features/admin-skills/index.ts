// Public surface of 技能管理. The route reads `:tab`, picks a page, and renders
// the column's own title/subtitle from the `admin-skills` namespace.
export { StorePage } from "./components/StorePage"
export { ReviewPage } from "./components/ReviewPage"
export { InstallsPage } from "./components/InstallsPage"
export { ADMIN_SKILL_TABS, isAdminSkillTab, type AdminSkillTab } from "./lib/tabs"
