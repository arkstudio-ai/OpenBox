import { useTranslation } from "react-i18next"
import type { PermissionRequest } from "@/shared/types/api"
import { useReplyPermission } from "../api/permission"
import { previewInput } from "../lib/tool-map"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"

/** Inline permission prompt: tool + input summary and allow / always / reject. */
export function PermissionCard({ request }: { request: PermissionRequest }) {
  const { t } = useTranslation("chat")
  const reply = useReplyPermission()
  const detail = previewInput(request.input)
  const errorMessage = useApiErrorMessage()
  const binding = request.assistant ? { assistant: request.assistant } : {}
  return (
    <div className="border-hair bg-card flex max-w-165 flex-col gap-3 rounded-xl border p-5">
      <div className="flex flex-col gap-1">
        <span className="text-base font-medium">{t("permission.title")}</span>
        <span className="text-n600 text-sm">{t("permission.body")}</span>
      </div>
      <div className="bg-n100 flex flex-col gap-1 rounded-lg p-3">
        <span className="text-ink font-mono text-sm">{request.title ?? request.tool}</span>
        {detail && <span className="text-n600 font-mono text-xs break-all">{detail}</span>}
        {request.assistant && <details className="text-xs">
          <summary className="cursor-pointer">{t("permission.details")}</summary>
          <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(request.input, null, 2)}</pre>
        </details>}
        {request.patterns?.length ? <p className="text-xs break-all">{t("permission.targets", { targets: request.patterns.join(", ") })}</p> : null}
        {request.assistant && <p className="text-xs break-all">{t("permission.alwaysScope", {
          tool: request.tool, patterns: (request.always ?? request.patterns ?? []).join(", "),
        })}</p>}
      </div>
      {reply.error && <p role="alert" className="text-danger text-sm">{errorMessage(reply.error)}</p>}
      <div className="flex flex-wrap gap-2.5">
        <button
          type="button"
          onClick={() => reply.mutate({ requestId: request.id, action: "allow", ...binding })}
          disabled={reply.isPending}
          className="bg-ink text-bg rounded-full px-4 py-1.5 text-sm disabled:opacity-60"
        >
          {t("permission.allow")}
        </button>
        <button
          type="button"
          onClick={() => reply.mutate({ requestId: request.id, action: "allow_always", ...binding })}
          disabled={reply.isPending}
          className="border-hair text-ink hover:bg-hairsoft rounded-full border px-4 py-1.5 text-sm"
        >
          {t("permission.allowAlways")}
        </button>
        <button
          type="button"
          onClick={() => reply.mutate({ requestId: request.id, action: "reject", ...binding })}
          disabled={reply.isPending}
          className="border-hair text-danger hover:bg-dangersoft rounded-full border px-4 py-1.5 text-sm"
        >
          {t("permission.deny")}
        </button>
      </div>
    </div>
  )
}
