import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { DiagnosticData, memoryCard } from "@/shared/ui/MemoryDiagnostics"
import { Spinner } from "@/shared/ui/Spinner"
import { useDebugHealth, type DebugCapabilities } from "./api"

const sections = ["index", "jobs", "outbox", "wiki"] as const

export function DebugHealth({ capabilities }: { capabilities?: DebugCapabilities }) {
  const { t } = useTranslation("memory")
  const errorText = useApiErrorMessage()
  const query = useDebugHealth()
  return (
    <details className={memoryCard}>
      <summary className="cursor-pointer text-sm font-medium">{t("debug.health")}</summary>
      <p className="text-n500 my-3 text-xs">{t("debug.healthHint")}</p>
      {query.error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(query.error)}
        </p>
      )}
      {query.isLoading && <Spinner />}
      {query.data && (
        <div className="grid gap-3 md:grid-cols-2">
          {sections.map((section) => (
            <section key={section} className="space-y-2">
              <h2 className="text-sm font-medium">{t(`debug.healthSection.${section}`)}</h2>
              <DiagnosticData data={query.data[section]} />
            </section>
          ))}
        </div>
      )}
      {capabilities && (
        <details className="mt-3">
          <summary className="text-n500 cursor-pointer text-xs">{t("debug.capabilities")}</summary>
          <DiagnosticData data={capabilities} />
        </details>
      )}
    </details>
  )
}
