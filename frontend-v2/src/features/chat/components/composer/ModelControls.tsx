import { useTranslation } from "react-i18next"
import { Clapperboard } from "lucide-react"
import { useAuthStore } from "@/shared/api/auth-store"
import type { useComposerModels } from "../../hooks/useComposerModels"
import { modelLabel } from "../../lib/model"
import { ModelLogo } from "../ModelLogo"
import { ModelMenuRows, ModelPicker } from "./ModelPicker"
import { ReasoningPicker } from "./ReasoningPicker"
import { TierPicker } from "./TierPicker"
import { VideoModelMenuRows, VideoModelPicker } from "./VideoModelPicker"

interface Props {
  choices: ReturnType<typeof useComposerModels>
}

/** The composer's model controls: chat model, its strength, video model.
 *
 *  With tiers declared, each catalogue folds into a three-tier pill. The
 *  catalogue behind it is an admin's view — everyone else reads the tiers and
 *  nothing about routing — and the strength picker goes with it: a tier
 *  already carries its strength, and a second knob beside it would make
 *  "three tiers" a lie. Without tiers, the plain pickers stay as they were.
 */
export function ModelControls({ choices }: Props) {
  const { t } = useTranslation("chat")
  const { models, videoModels, chat, video, reasoning, tiers } = choices
  const isAdmin = useAuthStore((s) => s.user?.role) === "admin"

  const chatTierOptions = tiers.chat.map((row) => ({
    tier: row.tier,
    label: t(`tier.chat.${row.tier}`),
    hint: modelLabel(row.model, models),
  }))
  const videoName = (id: string | undefined) => videoModels.find((m) => m.id === id)?.name ?? id ?? ""
  const videoTierOptions = tiers.video.map((row) => ({
    tier: row.tier,
    label: row.label || t(`tier.video.${row.tier}`),
    hint: videoName(row.model),
    description: row.description || undefined,
    // Each resolution with its per-second price, from the table the estimate
    // bills against. Unpriced ones show bare.
    chips: row.resolutions.map((resolution) => ({
      id: resolution,
      label: resolution,
      note: row.prices[resolution] ? t("tier.video.perSecond", { price: row.prices[resolution] }) : undefined,
    })),
  }))
  const videoFallbackLabel = [videoName(video.activeId), video.activeResolution ?? ""]
    .filter(Boolean)
    .join(" ")

  const chatControl =
    chatTierOptions.length > 0 ? (
      <TierPicker
        className="ms-auto"
        icon={<ModelLogo id={chat.activeId ?? ""} className="text-ink size-4 flex-none" />}
        title={t("tier.chat.pick")}
        options={chatTierOptions}
        activeTier={tiers.activeChat}
        fallbackLabel={chat.activeId ? modelLabel(chat.activeId, models) : ""}
        onPick={tiers.pickChat}
        catalogue={
          isAdmin
            ? (close) => (
                <ModelMenuRows
                  models={models}
                  activeId={chat.activeId}
                  onPick={(id) => {
                    chat.pick(id)
                    close()
                  }}
                />
              )
            : undefined
        }
      />
    ) : (
      <ModelPicker models={models} activeId={chat.activeId} onPick={chat.pick} />
    )

  const videoControl =
    videoTierOptions.length > 0 && videoModels.length > 0 ? (
      <TierPicker
        icon={<Clapperboard className="text-ink size-4 flex-none" />}
        title={t("tier.video.pick")}
        options={videoTierOptions}
        activeTier={tiers.activeVideo}
        activeChip={video.activeResolution}
        fallbackLabel={videoFallbackLabel}
        onPick={tiers.pickVideo}
        catalogue={
          isAdmin
            ? (close) => (
                <VideoModelMenuRows
                  models={videoModels}
                  activeId={video.activeId}
                  activeResolution={video.activeResolution}
                  onPick={(id, resolution) => {
                    video.pick(id, resolution)
                    close()
                  }}
                />
              )
            : undefined
        }
      />
    ) : (
      <VideoModelPicker
        models={videoModels}
        activeId={video.activeId}
        activeResolution={video.activeResolution}
        onPick={video.pick}
      />
    )

  return (
    <>
      {chatControl}
      {(chatTierOptions.length === 0 || isAdmin) && (
        <ReasoningPicker
          variants={reasoning.variants}
          activeId={reasoning.activeId}
          defaultId={reasoning.defaultId}
          onPick={reasoning.pick}
        />
      )}
      {/* Beside the chat model on purpose — the two are picked independently,
          and a person setting up a video turn expects to choose both in one
          place. */}
      {videoControl}
    </>
  )
}
