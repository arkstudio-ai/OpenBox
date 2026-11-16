import { useTranslation } from "react-i18next"
import { useAssetUrl } from "../api/assets"

export function VoiceMessageBubble({
  assetId,
  durationMs,
  transcript,
}: {
  assetId: string
  durationMs: number
  transcript: string
}) {
  const { t } = useTranslation("chat")
  const { data, error, refetch } = useAssetUrl(assetId)
  return (
    <div className="bg-n200/60 text-ink max-w-[88%] rounded-xl p-3">
      <div className="flex items-center gap-2">
        <span className="text-sm">{Math.ceil(durationMs / 1000)}″</span>
        {data?.url && (
          <audio
            aria-label={t("voiceMessage.switchVoice")}
            src={data.url}
            controls
            preload="none"
            className="h-10 max-w-full"
          />
        )}
        {error && (
          <button type="button" onClick={() => void refetch()}>
            {t("voiceMessage.playFailed")}
          </button>
        )}
      </div>
      <details className="text-n600 mt-2 text-sm">
        <summary className="cursor-pointer">{t("voiceMessage.showText")}</summary>
        <p className="mt-2 whitespace-pre-wrap">{transcript}</p>
      </details>
    </div>
  )
}
