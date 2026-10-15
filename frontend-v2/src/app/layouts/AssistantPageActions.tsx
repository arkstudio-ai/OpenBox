import { lazy, Suspense, useState } from "react"
import { Settings2 } from "lucide-react"
import { useTranslation } from "react-i18next"
import { VoiceCallButton, useVoiceCall } from "@/features/voice"
import { Sheet } from "@/shared/ui/Sheet"
import { Spinner } from "@/shared/ui/Spinner"

const QuickSettings = lazy(() =>
  import("@/features/settings").then((module) => ({ default: module.AssistantQuickSettings })),
)

/** The full conversation owns the chat; its header provides call and settings access. */
export function AssistantPageActions() {
  const { t } = useTranslation(["companion", "common"])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const { status } = useVoiceCall()
  const close = () => setSettingsOpen(false)
  return (
    <>
      <VoiceCallButton />
      <button
        type="button"
        onClick={() => setSettingsOpen(true)}
        aria-label={t("tabs.settings")}
        aria-haspopup="dialog"
        aria-expanded={settingsOpen}
        title={t("tabs.settings")}
        className="border-hair text-n800 hover:bg-hairsoft flex h-8 flex-none items-center gap-1.5 rounded-full border px-3 text-sm"
      >
        <Settings2 size={15} strokeWidth={2.2} aria-hidden />
        <span className="hidden sm:inline">{t("tabs.settings")}</span>
      </button>
      <Sheet
        open={settingsOpen}
        onClose={close}
        title={t("tabs.settings")}
        closeLabel={t("action.close", { ns: "common" })}
      >
        <Suspense
          fallback={
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          }
        >
          <QuickSettings inCall={status !== "idle" && status !== "ended"} onNavigate={close} />
        </Suspense>
      </Sheet>
    </>
  )
}
