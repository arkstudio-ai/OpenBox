// The composer's two independent model choices, resolved in one place.
//
// Chat model and video model are picked separately and mean different things —
// one retargets the next turn, the other only reaches video segments not yet
// submitted. What they share is the same fallback shape (unsent pick → the
// session's record → the deployment default), and doing that twice inline put
// Composer over the complexity ceiling for no benefit to the reader.
//
// Tiers sit on top of both: a deployment declares presets for each kind,
// each resolving to a real model. The
// picker shows the tier, the wire still carries the model — sessions,
// billing and the meta badges never learn the word "tier".
import type {
  AppConfig,
  ChatTierRow,
  ModelInfo,
  ModelTier,
  VideoTier,
  VideoModelInfo,
  VideoTierRow,
} from "@/shared/types/api"
import { useModelChoice } from "./useModelChoice"
import { useReasoningChoice } from "./useReasoningChoice"
import { useVideoModelChoice } from "./useVideoModelChoice"

// Stable identities, so a picker is not handed a fresh array on every render.
const EMPTY_MODELS: ModelInfo[] = []
const EMPTY_VIDEO_MODELS: VideoModelInfo[] = []
const EMPTY_CHAT_TIERS: ChatTierRow[] = []
const EMPTY_VIDEO_TIERS: VideoTierRow[] = []

interface Options {
  config?: AppConfig
  sessionModel?: string
  sessionVariant?: string | null
  sessionVideoModel?: string
  sessionVideoResolution?: string
  sessionKey?: string
}

export function useComposerModels({
  config,
  sessionModel,
  sessionVariant,
  sessionVideoModel,
  sessionVideoResolution,
  sessionKey,
}: Options) {
  const models = config?.models ?? EMPTY_MODELS
  const videoModels = config?.video_models ?? EMPTY_VIDEO_MODELS
  const chatTiers = config?.model_tiers?.chat ?? EMPTY_CHAT_TIERS
  const videoTiers = config?.model_tiers?.video ?? EMPTY_VIDEO_TIERS

  const chat = useModelChoice({
    sessionModel,
    sessionKey,
    fallback: config?.default_model ?? models[0]?.id,
  })
  const video = useVideoModelChoice({
    sessionVideoModel,
    sessionVideoResolution,
    sessionKey,
    fallback: config?.default_video_model ?? videoModels[0]?.id,
    resolutionFallback: config?.default_video_resolution,
    // Each model's own tiers, so a pick made on one model is not carried onto
    // another that does not offer it.
    resolutionsByModel: Object.fromEntries(videoModels.map((model) => [model.id, model.resolutions])),
  })
  const reasoning = useReasoningChoice({
    model: models.find((model) => model.id === chat.activeId),
    sessionModel,
    sessionVariant,
    sessionKey,
  })

  // Which tier the current choice falls in, if any. Matched on the model for
  // chat (an admin retuning the strength is still on that tier); for video
  // the model must match and the resolution must be one the tier offers,
  // since a tier is a model plus the resolutions it lets you pick.
  const activeChatTier = chatTiers.find((row) => row.model === chat.activeId)?.tier
  const activeVideoTier = videoTiers.find(
    (row) =>
      row.model === video.activeId &&
      (row.resolutions.length === 0 || row.resolutions.includes(video.activeResolution ?? "")),
  )?.tier

  const pickChatTier = (tier: ModelTier) => {
    const row = chatTiers.find((candidate) => candidate.tier === tier)
    if (!row) return
    chat.pick(row.model)
    // The strength travels with the tier. Stored against the tier's model,
    // not the one still active this render — see useReasoningChoice.pickFor.
    const target = models.find((model) => model.id === row.model)
    if (target) reasoning.pickFor(target, row.variant)
  }

  const pickVideoTier = (tier: VideoTier, resolution?: string) => {
    const row = videoTiers.find((candidate) => candidate.tier === tier)
    if (!row) return
    // A resolution the tier does not offer falls back to the tier's default,
    // so a stale chip never sends a pair the backend would refuse.
    const chosen = resolution && row.resolutions.includes(resolution) ? resolution : row.resolution
    video.pick(row.model, chosen)
  }

  return {
    models,
    videoModels,
    chat,
    video,
    reasoning,
    tiers: {
      chat: chatTiers,
      video: videoTiers,
      activeChat: activeChatTier,
      activeVideo: activeVideoTier,
      pickChat: pickChatTier,
      pickVideo: pickVideoTier,
    },
  }
}
