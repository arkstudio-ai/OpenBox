// Production components with isolated identity and intercepted HTTP only.
import { Suspense } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Routes, Route, Link } from "react-router"
import { StorePage } from "../../src/features/admin-skills/components/StorePage"
import { InstallsPage } from "../../src/features/admin-skills/components/InstallsPage"
import { useAuthStore } from "../../src/shared/api/auth-store"
import "../../src/styles/index.css"
import i18n from "../../src/shared/i18n"

await i18n.changeLanguage("zh-CN")
useAuthStore.setState({
  user: { id: "fixture-admin", username: "fixture-admin", role: "admin" } as NonNullable<
    ReturnType<typeof useAuthStore.getState>["user"]
  >,
})
const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <Suspense fallback={null}>
      <MemoryRouter initialEntries={["/store"]}>
        <main className="bg-bg text-ink min-h-dvh min-w-0 p-3 sm:p-6">
          <nav className="mb-4 flex gap-4">
            <Link to="/store">商店上架</Link>
            <Link to="/installs">用户安装</Link>
          </nav>
          <Routes>
            <Route path="/store" element={<StorePage />} />
            <Route path="/installs" element={<InstallsPage />} />
          </Routes>
        </main>
      </MemoryRouter>
    </Suspense>
  </QueryClientProvider>,
)
