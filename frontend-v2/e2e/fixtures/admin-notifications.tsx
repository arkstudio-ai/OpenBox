import { Suspense } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { NotificationsPage } from "../../src/features/admin-notifications"
import { useAuthStore } from "../../src/shared/api/auth-store"
import "../../src/styles/index.css"
import i18n from "../../src/shared/i18n"

await i18n.changeLanguage("zh-CN")
useAuthStore.setState({ user: { id: "fixture-admin", username: "Admin", role: "admin" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> })
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={new QueryClient()}><Suspense fallback={null}><main className="bg-bg text-ink min-h-dvh min-w-0 p-3 sm:p-6"><NotificationsPage /></main></Suspense></QueryClientProvider>)
