import { useTranslation } from "react-i18next"
import { ResultArtifacts } from "@/features/chat/components/ResultArtifacts"
import type { ArtifactGroup } from "@/features/chat/lib/content-view"
import type { TeamArtifactPreviewProps } from "@/features/agent-team/components/TeamPanel"

/** Compose the shared chat media renderer without coupling the two features. */
export function TeamArtifactPreview({ artifacts, finalIds }: TeamArtifactPreviewProps) {
  const { t } = useTranslation("teams")
  const groups: ArtifactGroup[] = artifacts.flatMap((artifact, order) => {
    const asset = artifact.asset
    if (!asset) return []
    const final = finalIds.includes(artifact.id)
    const artifactKind = asset.mime.startsWith("video/") && final ? "video_final" : "shared_file"
    return [
      {
        kind: "artifact",
        id: artifact.id,
        order,
        artifactKind,
        role: final ? "final" : "result",
        label: artifact.name || artifact.title || asset.name,
        caption: artifact.summary || artifact.content || null,
        ordinal: null,
        revision: null,
        metadata: {},
        sourceTool: null,
        parts: [
          {
            type: "file",
            id: artifact.id,
            path: asset.name,
            mime_type: asset.mime,
            asset_id: asset.id,
            size: asset.size,
          },
        ],
      },
    ]
  })
  return (
    <>
      <ResultArtifacts groups={groups} verification={null} />
      {artifacts
        .filter((artifact) => !artifact.asset)
        .map((artifact) => (
          <article key={artifact.id} className="border-hair bg-card rounded-xl border p-3">
            <strong className="text-sm">
              {artifact.name || artifact.title || artifact.path || artifact.id}
            </strong>
            <p className="text-n600 mt-2 text-xs whitespace-pre-wrap">
              {artifact.summary || artifact.content}
            </p>
            {artifact.file_asset_id && <p className="text-n600 mt-2 text-xs">{t("artifactUnavailable")}</p>}
          </article>
        ))}
    </>
  )
}
