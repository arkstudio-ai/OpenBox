import { useState } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ResultArtifacts } from "../../src/features/chat/components/ResultArtifacts"
import { buildAssistantContentView } from "../../src/features/chat/lib/content-view"
import type { FilePart, MessageWithParts } from "../../src/shared/types/api"
import directTranscript from "../../src/features/chat/lib/__fixtures__/direct-video-messages.json"
import "../../src/styles/index.css"
import "../../src/shared/i18n"

const segment = (ordinal: number): FilePart => ({
  type: "file",
  id: `segment-${ordinal}`,
  path: `segment-${ordinal}.mp4`,
  asset_id: `segment-${ordinal}`,
  mime_type: "video/mp4",
  relation: {
    kind: "video_segment",
    role: "intermediate",
    ordinal,
    caption: "分段口播素材，保留原始顺序，支持展开预览。",
  },
})
const finalVideo: FilePart = {
  type: "file",
  id: "final",
  path: "final.mp4",
  asset_id: "final",
  mime_type: "video/mp4",
  relation: { kind: "video_final", role: "final", label: "最终视频" },
}
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function directMessages(state: string): MessageWithParts[] {
  const messages = structuredClone(directTranscript) as MessageWithParts[]
  if (state === "generating") return messages.slice(0, 1)
  if (state === "preview" || state === "failed") return messages.slice(0, 2)
  return messages
}

export function Fixture() {
  const [state, setState] = useState(
    new URLSearchParams(location.search).get("final") ? "final" : "generating",
  )
  // Deliberately deliver segments out of order to exercise the real projection.
  const parts = [segment(3), segment(1), segment(2), ...(state === "final" ? [finalVideo] : [])]
  const direct = new URLSearchParams(location.search).has("direct")
  const view = buildAssistantContentView(
    direct ? directMessages(state) : [{ id: "m", session_id: "s", role: "assistant", created_at: "", parts }],
    state === "generating" || state === "preview",
  )
  return (
    <main className="bg-bg text-ink min-h-dvh p-4 sm:p-8">
      <header className="mb-4 flex flex-wrap gap-3">
        <button onClick={() => setState("final")}>模拟成片</button>
        {direct ? <button onClick={() => setState("preview")}>模拟附件预览</button> : null}
        <button onClick={() => setState("failed")}>模拟合成失败</button>
        <button onClick={() => setState("generating")}>移除成片</button>
        <span role="status">{state}</span>
      </header>
      <div className="mx-auto max-w-3xl">
        <ResultArtifacts groups={view.resultGroups} verification={null} />
      </div>
    </main>
  )
}
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <Fixture />
  </QueryClientProvider>,
)
