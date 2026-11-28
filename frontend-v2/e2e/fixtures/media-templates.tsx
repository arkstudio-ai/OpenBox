import { Suspense } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes } from "react-router"
import SkillsRoute from "../../src/routes/skills/SkillsRoute"
import AdminExamplesRoute from "../../src/routes/admin/AdminExamplesRoute"
import { useAuthStore } from "../../src/shared/api/auth-store"
import { useWorkspaceStore } from "../../src/shared/api/workspace-store"
import i18n from "../../src/shared/i18n"
import "../../src/styles/index.css"

await i18n.changeLanguage("zh-CN")
useAuthStore.getState().setAuth("fixture", { id: "creator", username: "creator", role: "admin" })
useWorkspaceStore.setState({ currentId: "ws-fixture" })
const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
const initial = location.search.includes("admin") ? "/app/admin/examples" : "/app/skills?section=media"
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <Suspense>
      <MemoryRouter initialEntries={[initial]}>
        <main className="bg-bg text-ink flex min-h-dvh min-w-0 flex-col p-4 sm:p-8">
          <Routes>
            <Route path="/app/skills" element={<SkillsRoute />} />
            <Route path="/app/admin/examples" element={<AdminExamplesRoute />} />
          </Routes>
        </main>
      </MemoryRouter>
    </Suspense>
  </QueryClientProvider>,
)
