// The composer's two independent model choices, resolved in one place.
//
// Chat model and video model are picked separately and mean different things —
// one retargets the next turn, the other only reaches video segments not yet
// submitted. What they share is the same fallback shape (unsent pick → the
// session's record → the deployment default), and doing that twice inline put
// Composer over the complexity ceiling for no benefit to the reader.
import type { AppConfig, ModelInfo, VideoModelInfo } from "@/shared/types/api"
import { useModelChoice } from "./useModelChoice"
import { useReasoningChoice } from "./useReasoningChoice"
import { useVideoModelChoice } from "./useVideoModelChoice"

// Stable identities, so a picker is not handed a fresh array on every render.
const EMPTY_MODELS: ModelInfo[] = []
const EMPTY_VIDEO_MODELS: VideoModelInfo[] = []

interface Options {
  config?: AppConfig
  sessionModel?: string
  sessionVariant?: string | null
  sessionVideoModel?: string
  sessionKey?: string
}

export function useComposerModels({
  config,
  sessionModel,
  sessionVariant,
  sessionVideoModel,
  sessionKey,
}: Options) {
  const models = config?.models ?? EMPTY_MODELS
  const videoModels = config?.video_models ?? EMPTY_VIDEO_MODELS

  const chat = useModelChoice({
    sessionModel,
    sessionKey,
    fallback: config?.default_model ?? models[0]?.id,
  })
  const video = useVideoModelChoice({
    sessionVideoModel,
    sessionKey,
    fallback: config?.default_video_model ?? videoModels[0]?.id,
  })
  const reasoning = useReasoningChoice({
    model: models.find((model) => model.id === chat.activeId),
    sessionModel,
    sessionVariant,
    sessionKey,
  })

  return { models, videoModels, chat, video, reasoning }
}
