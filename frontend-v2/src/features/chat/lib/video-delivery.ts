import type { FilePart } from "@/shared/types/api"
import type { ArtifactGroup } from "./content-view"

function uploadedVideo(part: FilePart): boolean {
  return Boolean(part.asset_id?.trim()) && Boolean(part.mime_type?.startsWith("video/"))
}

function outputValue(group: ArtifactGroup, key: string): string | undefined {
  return (
    group.sourceTool?.output
      ?.split("\n")
      .find((line) => line.startsWith(`${key}=`))
      ?.slice(key.length + 1)
      .trim() || undefined
  )
}

export function isFinalVideoArtifact(group: ArtifactGroup): boolean {
  return (
    group.artifactKind !== "video_segment" &&
    (group.artifactKind === "video_final" || group.role === "final") &&
    group.parts.some(uploadedVideo)
  )
}

function standaloneSegment(group: ArtifactGroup): boolean {
  const tool = group.sourceTool
  if (tool?.tool !== "video_generate" || tool.status !== "completed") return false
  if (outputValue(group, "status") !== "completed") return false
  // A storyboard preview is still an intermediate even if it is shared.
  return (
    !["production_id", "segment_id"].some((key) =>
      Boolean(group.metadata[key] || tool.input?.[key] || outputValue(group, key)),
    ) && group.parts.some(uploadedVideo)
  )
}

function deliveredCopy(group: ArtifactGroup, segment: ArtifactGroup): boolean {
  const tool = group.sourceTool
  if (group.artifactKind !== "shared_file" || group.role !== "result" || group.order <= segment.order)
    return false
  if (
    tool?.tool !== "share_file" ||
    tool.status !== "completed" ||
    tool.metadata?.attached === false ||
    tool.input?.attach === false
  )
    return false
  if (!group.parts.length || !group.parts.every(uploadedVideo)) return false
  const sourceAssets = new Set(segment.parts.filter(uploadedVideo).map((part) => part.asset_id))
  const workspacePath = outputValue(segment, "workspace_path")
  // share_file can re-upload the same output under a new asset ID. Match the
  // recorded workspace path, never a basename or a guessed "final" filename.
  return group.parts.every(
    (part) => sourceAssets.has(part.asset_id) || (Boolean(workspacePath) && part.path === workspacePath),
  )
}

/** Compatibility projection for single-shot generation -> share_file -> final
 * answer. It changes no stored parts/assets. Until the turn completes, a plain
 * result attachment is not sufficient evidence of final delivery. Explicit
 * video_final/role=final artifacts keep their immediate streaming behaviour. */
export function resolveDirectVideoDelivery(groups: ArtifactGroup[], completed: boolean): ArtifactGroup[] {
  if (!completed || groups.some(isFinalVideoArtifact)) return groups
  const segments = groups.filter((group) => group.artifactKind === "video_segment")
  if (segments.length !== 1 || !standaloneSegment(segments[0])) return groups
  const delivery = groups.filter((group) => deliveredCopy(group, segments[0])).at(-1)
  return groups.map((group) =>
    group === delivery ? { ...group, artifactKind: "video_final", role: "final" } : group,
  )
}
