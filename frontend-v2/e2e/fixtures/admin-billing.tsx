import { Suspense } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { WorkspaceDetailPage } from "../../src/features/admin-billing/components/WorkspaceDetailPage"
import { useAuthStore } from "../../src/shared/api/auth-store"
import "../../src/styles/index.css"
import i18n from "../../src/shared/i18n"

await i18n.changeLanguage("zh-CN")
useAuthStore.setState({
  user: { id: "fixture-admin", username: "e", role: "admin" } as NonNullable<
    ReturnType<typeof useAuthStore.getState>["user"]
  >,
})
const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <Suspense fallback={null}>
      <MemoryRouter>
        <main className="bg-bg text-ink min-h-dvh p-4 sm:p-8">
          <WorkspaceDetailPage workspaceId="target" />
        </main>
      </MemoryRouter>
    </Suspense>
  </QueryClientProvider>,
)
