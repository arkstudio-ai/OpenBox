import { useTranslation } from "react-i18next"
import { formatDateTime, formatNumber } from "@/shared/lib/format"
import { DiagnosticData, MemoryStatus, memoryCard } from "@/shared/ui/MemoryDiagnostics"
import type { DebugStep as Step } from "./api"

const usageFields = ["input_tokens", "output_tokens", "estimated_cost", "billed_cost"] as const

const decisionFields = ["choice", "confidence", "threshold"] as const

function RouteDecision({ name, data }: { name: string; data: unknown }) {
  const { t } = useTranslation("memory")
  const decision = data && typeof data === "object" ? (data as Record<string, unknown>) : null
  return (
    <div className="border-hair space-y-2 rounded-lg border p-3">
      <h4 className="text-sm font-medium">{t(`debug.decision.${name}`, { defaultValue: name })}</h4>
      {decision ? (
        <>
          <dl className="grid grid-cols-2 gap-2 text-xs">
            {decisionFields.map((key) => (
              <div key={key}>
                <dt className="text-n500">{t(`debug.${key}`)}</dt>
                <dd className="mt-1 break-words">
                  {decision[key] == null ? t("unknown") : String(decision[key])}
                </dd>
              </div>
            ))}
          </dl>
          <DiagnosticData data={decision.probabilities} label={t("debug.probabilities")} />
        </>
      ) : (
        <p className="text-n500 text-sm">{t("unknown")}</p>
      )}
    </div>
  )
}

function StepUsage({ usage }: { usage: Record<string, unknown> | null | undefined }) {
  const { t } = useTranslation("memory")
  const number = (value: unknown) =>
    typeof value === "number" ? formatNumber(value) : value == null ? t("unknown") : String(value)
  return (
    <div className="mt-3 space-y-2">
      <h4 className="text-sm font-medium">{t("debug.usage")}</h4>
      <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        {usageFields.map((key) => (
          <div key={key}>
            <dt className="text-n500">{t(`debug.${key}`)}</dt>
            <dd className="mt-1">{number(usage?.[key])}</dd>
          </div>
        ))}
      </dl>
      {usage && (
        <details>
          <summary className="text-n500 cursor-pointer text-xs">{t("debug.usageDetails")}</summary>
          <DiagnosticData data={usage} />
        </details>
      )}
    </div>
  )
}

export function DebugStep({ step, order }: { step: Step; order: number }) {
  const { t } = useTranslation("memory")
  const data = step.data
  const route = step.phase === "route" || step.phase === "routing"
  return (
    <li className={`${memoryCard} space-y-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">
          {order} · {t(`debug.phase.${step.phase}`, { defaultValue: step.phase })}
        </h3>
        <MemoryStatus status={step.status} />
      </div>
      <dl className="text-n600 flex flex-wrap gap-x-5 gap-y-2 text-xs">
        <div>
          <dt className="inline">{t("debug.duration")}</dt>
          <dd className="ms-2 inline">
            {step.duration_ms == null ? t("unknown") : t("debug.milliseconds", { count: step.duration_ms })}
          </dd>
        </div>
        <div>
          <dt className="inline">{t("debug.reason")}</dt>
          <dd className="ms-2 inline">
            {step.reason_code
              ? t(`debug.reasonCode.${step.reason_code}`, { defaultValue: step.reason_code })
              : t("unknown")}
          </dd>
        </div>
        {(step.started_at ?? step.created_at) && (
          <div>
            <dt className="inline">{t("debug.started")}</dt>
            <dd className="ms-2 inline">{formatDateTime(step.started_at ?? step.created_at!)}</dd>
          </div>
        )}
        {step.finished_at && (
          <div>
            <dt className="inline">{t("debug.finished")}</dt>
            <dd className="ms-2 inline">{formatDateTime(step.finished_at)}</dd>
          </div>
        )}
      </dl>
      {route && data && (
        <div className="grid gap-3 sm:grid-cols-2">
          <RouteDecision
            name="memory"
            data={data.memory ?? (data.decisions as Record<string, unknown> | undefined)?.memory}
          />
          <RouteDecision
            name="task"
            data={data.task ?? (data.decisions as Record<string, unknown> | undefined)?.task}
          />
        </div>
      )}
      <DiagnosticData data={data} label={t("debug.stepData")} />
      <StepUsage usage={step.usage} />
    </li>
  )
}
