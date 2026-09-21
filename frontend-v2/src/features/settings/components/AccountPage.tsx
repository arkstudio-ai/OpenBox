import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { RowList, Row, ValueRow } from "./SettingsRow"
import { useAgentConfig, usePreferences, useUpdatePreferences } from "../api/settings"

function Value({ children }: { children: string }) {
  return <span className="text-n700 flex-none text-sm">{children}</span>
}

export function AccountPage() {
  const { t } = useTranslation("settings")
  const user = useAuthStore((s) => s.user)
  const config = useAgentConfig()
  const preferences = usePreferences()
  const update = useUpdatePreferences()
  if (!user) return null
  return (
    <RowList>
      <Row label={t("account.username")} right={<Value>{user.username}</Value>} />
      <Row label={t("account.email")} right={<Value>{user.email || t("account.emailNone")}</Value>} />
      <Row label={t("account.role")} right={<Value>{user.role}</Value>} />
      <ValueRow label={t("account.userId")} value={user.id} mono />
      {config.data?.team_ui_enabled && (
        <div className="border-hair border-t pt-5">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              className="accent-ink mt-1 size-4 shrink-0"
              checked={preferences.data?.agent_autoapprove_t0 === true}
              disabled={!preferences.data || update.isPending}
              onChange={(event) => update.mutate({ agent_autoapprove_t0: event.target.checked })}
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{t("account.agentAutoapprove")}</span>
              <span className="text-n600 mt-1 block text-xs leading-5">
                {t("account.agentAutoapproveHint")}
              </span>
            </span>
          </label>
          {(preferences.error || update.error) && (
            <p role="alert" className="text-danger mt-2 text-xs">
              {preferences.error?.message || update.error?.message}
            </p>
          )}
        </div>
      )}
    </RowList>
  )
}
