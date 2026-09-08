import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { MoreHorizontal } from "lucide-react"
import { env } from "@/shared/config/env"
import { useAuthStore } from "@/shared/api/auth-store"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { paths } from "@/shared/router/paths"
import { useCreditBalance } from "@/shared/api/billing"
import { formatCredits } from "@/shared/lib/format"

export function UserRow({ sessionCount }: { sessionCount: number }) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const [menuOpen, setMenuOpen] = useState(false)
  const credits = useCreditBalance()

  const signOut = () => {
    setMenuOpen(false)
    // One full-page navigation lets the backend revoke the OpenBox cookie and
    // immediately redirect to Logto. Clearing the store here would make the
    // auth guard mount /login and race a new SSO request against sign-out.
    window.location.assign(`${env.apiBase}/api/auth/logto/logout`)
  }

  return (
    <div className="relative mt-1 flex-none">
      <Menu open={menuOpen} onClose={() => setMenuOpen(false)} className="start-1 end-1 bottom-11.5 z-25">
        <MenuItem
          onClick={() => {
            setMenuOpen(false)
            navigate(paths.settings())
          }}
        >
          {t("settings")}
        </MenuItem>
        {user?.role === "admin" && (
          <MenuItem
            onClick={() => {
              setMenuOpen(false)
              navigate(paths.admin)
            }}
          >
            {t("adminConsole")}
          </MenuItem>
        )}
        <MenuItem onClick={signOut}>{t("common:action.signOut", { ns: "common" })}</MenuItem>
      </Menu>
      <div className="hover:bg-n200 flex items-center gap-2.5 rounded-full px-2.5 py-2">
        <span
          className="bg-n800 text-bg flex size-7 flex-none items-center justify-center rounded-full text-sm font-semibold uppercase"
          aria-hidden
        >
          {(user?.username ?? "?").slice(0, 1)}
        </span>
        <button
          type="button"
          className="flex min-w-0 flex-1 flex-col text-start leading-snug"
          title={t("viewCredits")}
          onClick={() => navigate(paths.billing("usage"))}
        >
          <span className="text-md truncate font-medium">{user?.username}</span>
          <span className="text-n600 truncate text-xs">
            {credits.isError
              ? t("creditsUnavailable")
              : t("creditBalance", { value: formatCredits(credits.data?.balance) })}
          </span>
          <span className="sr-only">
            {t("userLine", { role: user?.role ?? "user", count: sessionCount })}
          </span>
        </button>
        <button
          type="button"
          title={t("common:action.more", { ns: "common" })}
          aria-label={t("common:action.more", { ns: "common" })}
          className="text-n600 flex size-6 flex-none items-center justify-center rounded-full"
          onClick={() => setMenuOpen((v) => !v)}
        >
          <MoreHorizontal size={15} strokeWidth={2.4} />
        </button>
      </div>
    </div>
  )
}
