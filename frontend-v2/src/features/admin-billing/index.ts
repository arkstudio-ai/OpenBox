export { SubscriptionsPage } from "./components/SubscriptionsPage"
export { OrdersPage } from "./components/OrdersPage"
export { WorkspaceDetailPage } from "./components/WorkspaceDetailPage"

/** Tab ids for `/app/admin/billing/:tab?`; the first is the default. */
export const ADMIN_BILLING_TABS = ["subscriptions", "orders"] as const
