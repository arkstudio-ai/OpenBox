import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AiDisclosure, AiGeneratedLabel } from "../../src/features/chat/components/AiDisclosure"
import { AttachmentGallery } from "../../src/features/chat/components/AttachmentGallery"
import { useAuthStore } from "../../src/shared/api/auth-store"
import type { FilePart } from "../../src/shared/types/api"
import "../../src/styles/index.css"
import "../../src/shared/i18n"

// Local visual fixtures use the real preview components and no network assets.
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const userId = useAuthStore.getState().user?.id ?? "anonymous"
const dark = new URLSearchParams(location.search).has("dark")
document.documentElement.dataset.mode = dark ? "dark" : "light"
const landscape = (light: boolean) =>
  `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
  <defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="${light ? "#e9dfd0" : "#193139"}"/><stop offset="1" stop-color="${light ? "#f9f4e7" : "#77999b"}"/></linearGradient></defs>
  <rect width="960" height="540" fill="url(#sky)"/><circle cx="700" cy="145" r="60" fill="${light ? "#fffaf0" : "#f5d6ab"}"/>
  <path d="M0 350 180 195 370 345 540 265 770 365 960 270V540H0Z" fill="${light ? "#d1c9bb" : "#42646a"}"/>
  <path d="M0 390 155 350 310 420 550 340 760 395 960 370V540H0Z" fill="${light ? "#e4ddcf" : "#2d4e53"}"/>
  <path d="M0 455Q250 380 480 450T960 435V540H0Z" fill="${light ? "#f5eee1" : "#16393e"}"/>
  </svg>`)}`
const parts: FilePart[] = [false, true].map((light, index) => {
  const assetId = `aigc-fixture-${index}`
  client.setQueryData(["asset-url", userId, assetId], { url: landscape(light) })
  return {
    type: "file",
    id: assetId,
    asset_id: assetId,
    path: light ? "浅色画面.svg" : "深色画面.svg",
    mime_type: "image/svg+xml",
    relation: { kind: "generated_image" },
  }
})

export function Fixture() {
  return (
    <main className="bg-bg text-ink min-h-dvh px-5 py-8 sm:px-12 sm:py-12">
      <div className="mx-auto flex max-w-190 flex-col gap-6">
        <header className="border-hair flex items-center justify-between border-b pb-5">
          <span className="text-lg font-medium">BossIP</span>
          <span className="text-n600 text-sm">AI 标识 · 真实组件预览</span>
        </header>
        <div className="bg-n200 self-end rounded-2xl px-5 py-3 text-base">请帮我设计两张山野主题的封面。</div>
        <section className="space-y-3">
          <AiGeneratedLabel />
          <p>两种色调的封面方案：沉静的山林，与柔和的晨光。</p>
          <AttachmentGallery parts={parts} className="max-w-full" />
          <p className="text-n600 text-sm">以上为本地示意素材，用于检查深浅画面中的水印。点击可放大。</p>
        </section>
        <section className="border-hair rounded-2xl border p-4">
          <p className="text-n700 mb-3 text-sm">用户上传的原图 · 不标记为 AI 生成</p>
          <AttachmentGallery parts={[{ ...parts[1]!, id: "original", relation: undefined }]} />
        </section>
        <footer>
          <AiDisclosure />
        </footer>
      </div>
    </main>
  )
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <Fixture />
    </QueryClientProvider>
  </StrictMode>,
)
