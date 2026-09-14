import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { RowList, Row, ValueRow } from "./SettingsRow"

function Value({ children }: { children: string }) {
  return <span className="text-n700 flex-none text-sm">{children}</span>
}

export function AccountPage() {
  const { t } = useTranslation("settings")
  const user = useAuthStore((s) => s.user)
  if (!user) return null
  return (
    <RowList>
      <Row label={t("account.username")} right={<Value>{user.username}</Value>} />
      <Row label={t("account.email")} right={<Value>{user.email || t("account.emailNone")}</Value>} />
      <Row label={t("account.role")} right={<Value>{user.role}</Value>} />
      <ValueRow label={t("account.userId")} value={user.id} mono />
    </RowList>
  )
}
