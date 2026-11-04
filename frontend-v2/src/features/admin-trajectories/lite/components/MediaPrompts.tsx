// The prompts a generation model received (lite plan §15): the exact text the
// AI sent to video_generate (submit) or image_gen, with the settings beside it,
// so an operator never has to dig through a tool's JSON or a collapsed sub-task.
import type { TFunction } from "i18next"
import { useTranslation } from "react-i18next"
import { Clapperboard, ExternalLink, Image as ImageIcon } from "lucide-react"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { StatusPill } from "@/shared/ui/StatusPill"
import { CopyButton } from "../../components/inspector/CopyButton"
import { labelKey, RECORD_STATUS_LABELS, RECORD_STATUS_TONES } from "../../constants/labels"
import type { MediaPrompt } from "../types"
import { Clock } from "./bits"

const SETTING_ORDER = [
  "model",
  "resolution",
  "ratio",
  "duration",
  "generate_audio",
  "seed",
  "size",
  "quality",
  "n",
  "output_format",
]

function settingValue(key: string, value: string | number | boolean, t: TFunction<"admin-trajectories">) {
  if (typeof value === "boolean") return value ? t("common.yes") : t("common.no")
  if (key === "duration") return t("lite.prompts.seconds", { count: Number(value) })
  return String(value)
}

/** One generation request: what it was, its settings, and the prompt in full. */
export function PromptCard({
  prompt,
  sub,
  rawHref,
  compact = false,
}: {
  prompt: MediaPrompt
  /** Sent by a sub-task rather than the main assistant. */
  sub?: boolean
  rawHref?: string
  compact?: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  const video = prompt.medium === "video"
  const Icon = video ? Clapperboard : ImageIcon
  const settings = SETTING_ORDER.filter((key) => key in prompt.settings)
  const failed = prompt.status && ["failed", "denied", "timed_out"].includes(prompt.status)
  return (
    <article
      className={cn(
        "border-hair bg-card flex flex-col gap-2.5 rounded-xl border p-3.5",
        failed && "border-dangerink/40",
      )}
      data-testid="lite-media-prompt"
    >
      <header className="flex flex-wrap items-center gap-2">
        <Icon size={14} className="text-a700" aria-hidden />
        <span className="text-ink text-sm font-medium">
          {video ? t("lite.prompts.video") : t("lite.prompts.image")}
          {prompt.shot ? ` · ${t("lite.prompts.shot", { n: prompt.shot })}` : ""}
        </span>
        {sub && (
          <span className="bg-hairsoft text-n600 text-2xs rounded-full px-2 py-0.5">
            {t("lite.prompts.bySub")}
          </span>
        )}
        {prompt.status && (
          <StatusPill
            tone={RECORD_STATUS_TONES[prompt.status] ?? "muted"}
            title={prompt.status_reason ?? undefined}
          >
            {t(labelKey(RECORD_STATUS_LABELS, prompt.status, "status.other"), { value: prompt.status })}
          </StatusPill>
        )}
        <span className="text-n500 text-2xs">
          <Clock iso={prompt.started_at} />
        </span>
        <span className="flex-1" />
        {prompt.prompt && <CopyButton text={prompt.prompt} label={t("lite.prompts.copy")} />}
        {rawHref && (
          <Link
            to={rawHref}
            className="text-n500 hover:text-ink inline-flex items-center"
            title={t("lite.turn.rawRecord")}
            aria-label={t("lite.turn.rawRecord")}
          >
            <ExternalLink size={12} aria-hidden />
          </Link>
        )}
      </header>

      {(settings.length > 0 || prompt.inputs.length > 0 || video) && (
        <dl className="text-n700 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {settings.map((key) => (
            <div key={key} className="flex gap-1">
              <dt className="text-n500">{t(`lite.prompts.setting.${key}`, { defaultValue: key })}</dt>
              <dd className="font-medium">{settingValue(key, prompt.settings[key]!, t)}</dd>
            </div>
          ))}
          {video && !("model" in prompt.settings) && (
            <div className="flex gap-1">
              <dt className="text-n500">{t("lite.prompts.setting.model")}</dt>
              <dd>{t("lite.prompts.composerChoice")}</dd>
            </div>
          )}
          {prompt.inputs.length > 0 && (
            <div className="flex min-w-0 gap-1">
              <dt className="text-n500">{t("lite.prompts.inputs")}</dt>
              <dd className="min-w-0 break-all" title={prompt.inputs.map((item) => item.asset_id).join("\n")}>
                {prompt.inputs
                  .map((item) =>
                    t(`lite.prompts.role.${item.role ?? "reference"}`, { defaultValue: item.role ?? "" }),
                  )
                  .join("、")}
              </dd>
            </div>
          )}
        </dl>
      )}

      {prompt.prompt ? (
        <p
          className={cn(
            "bg-surface text-ink rounded-lg px-3 py-2.5 text-sm leading-6 break-words whitespace-pre-wrap",
            compact && "max-h-72 overflow-y-auto",
          )}
          data-testid="lite-media-prompt-text"
        >
          {prompt.prompt}
        </p>
      ) : (
        <p className="text-n500 text-xs">{t("lite.prompts.unavailable")}</p>
      )}

      {prompt.result_preview && (
        <p className="text-n600 text-xs break-words">
          <span className="text-n500">{t("lite.turn.result")}：</span>
          {prompt.result_preview}
        </p>
      )}
    </article>
  )
}

/** Every generation prompt of one turn, sub-tasks included, above the step flow. */
export function MediaPromptsSection({
  prompts,
  mainAgentId,
  rawHref,
}: {
  prompts: MediaPrompt[]
  mainAgentId: string | null
  rawHref: (recordId: string) => string
}) {
  const { t } = useTranslation("admin-trajectories")
  if (!prompts.length) return null
  return (
    <section
      className="flex flex-col gap-2.5"
      aria-label={t("lite.prompts.title")}
      data-testid="lite-media-prompts"
    >
      <h3 className="text-ink text-sm font-semibold">
        {t("lite.prompts.title")}
        <span className="text-n500 ms-2 text-xs font-normal">
          {t("lite.prompts.lead", { count: prompts.length })}
        </span>
      </h3>
      {prompts.map((prompt) => (
        <PromptCard
          key={prompt.record_id}
          prompt={prompt}
          sub={!!mainAgentId && !!prompt.agent_id && prompt.agent_id !== mainAgentId}
          rawHref={rawHref(prompt.record_id)}
        />
      ))}
    </section>
  )
}
