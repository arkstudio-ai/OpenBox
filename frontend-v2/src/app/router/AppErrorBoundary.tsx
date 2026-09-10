import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { Link, useRouteError } from "react-router"
import { paths } from "@/app/router/paths"
import { isChunkLoadError, recoverChunkLoadError, reloadPage } from "@/shared/lib/chunk-recovery"
import { reloadIfStale } from "@/shared/lib/build-version"

export function AppErrorBoundary() {
  const { t } = useTranslation("common")
  const error = useRouteError()
  const chunkFailure = isChunkLoadError(error)
  useEffect(() => {
    console.error("[route error]", error)
    // A missing chunk reloads once. Any other error on a tab the server has
    // already out-built is treated the same way: the fix is the new build,
    // not the person pressing refresh.
    if (!recoverChunkLoadError(error)) void reloadIfStale()
  }, [error])
  return (
    <div
      className="bg-bg flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center"
      role="alert"
    >
      <h1 className="text-ink text-2xl font-medium">{t(chunkFailure ? "pageLoad.title" : "state.error")}</h1>
      <p className="text-n600 max-w-md text-sm leading-6">
        {t(chunkFailure ? "pageLoad.body" : "pageLoad.genericBody")}
      </p>
      <div className="flex flex-wrap justify-center gap-3">
        <button
          type="button"
          onClick={reloadPage}
          className="bg-ink text-bg text-md min-h-11 rounded-full px-5 py-2"
        >
          {t("action.reload")}
        </button>
        <Link
          to={paths.app}
          className="border-hair text-ink text-md inline-flex min-h-11 items-center rounded-full border px-5 py-2"
        >
          {t("action.back")}
        </Link>
      </div>
    </div>
  )
}
