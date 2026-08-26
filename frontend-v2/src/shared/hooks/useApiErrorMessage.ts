import { useCallback } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"

/** Maps a thrown error to user copy via the errors namespace (§10.7). */
export function useApiErrorMessage() {
  const { t } = useTranslation("errors")
  return useCallback(
    (err: unknown): string => {
      if (err instanceof ApiError) {
        const byCode = t(err.code, { defaultValue: "" })
        if (byCode) return byCode
        const byStatus = t(`HTTP_${err.status}`, { defaultValue: "" })
        if (byStatus) return byStatus
        // Before giving up, use what the server said. Quota and validation
        // replies carry a specific reason ("Session quota exceeded: 200/200"),
        // and dropping it for a generic fallback hides the one detail that
        // tells someone what to do about it.
        const detail = err.message?.trim()
        if (detail && detail !== err.code) return detail
        return t("fallback")
      }
      if (err instanceof TypeError) return t("network")
      return t("fallback")
    },
    [t],
  )
}
