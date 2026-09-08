// State and handlers for the 云电脑登录态 card: 去登录 + polling, 全部检测, 退出登录.
import { useCallback, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { requestDesktopPanel } from "@/shared/events/desktop"
import {
  useLogoutDesktopLogin,
  useOpenDesktopLogin,
  useProbeAccount,
  useProbeDesktopLogins,
} from "../api/platform-accounts"
import { useLoginPolling } from "../components/DesktopLoginCard"
import type { PlatformAccount } from "../types"

export function useDesktopLogin(fail: (e: unknown) => void) {
  const { t } = useTranslation("auth-center")
  const probe = useProbeAccount()
  const openLogin = useOpenDesktopLogin()
  const probeDesktop = useProbeDesktopLogins()
  const logoutDesktop = useLogoutDesktopLogin()
  const [pendingLogout, setPendingLogout] = useState<PlatformAccount | null>(null)
  const [awaiting, setAwaiting] = useState<{ site: string; accountId: string } | null>(null)

  const probeOnce = useCallback((id: string) => probe.mutateAsync(id), [probe])
  const onPollDone = useCallback(
    (result: "bound" | "timeout") => {
      setAwaiting(null)
      if (result === "bound") toast.success(t("toast.desktopLoggedIn"))
      else toast.warning(t("toast.desktopLoginTimeout"))
    },
    [t],
  )
  useLoginPolling(awaiting, probeOnce, onPollDone)

  const onOpenLogin = (site: string) => {
    openLogin.mutate(site, {
      onSuccess: (row) => {
        setAwaiting({ site, accountId: row.id })
        requestDesktopPanel()
        toast.info(t("toast.desktopLoginOpened"))
      },
      onError: fail,
    })
  }

  const onProbeAll = () => {
    probeDesktop.mutate(undefined, { onError: fail })
  }

  const confirmLogout = () => {
    const target = pendingLogout
    if (!target) return
    setPendingLogout(null)
    logoutDesktop.mutate(target.id, {
      onSuccess: () => toast.success(t("toast.desktopLoggedOut")),
      onError: fail,
    })
  }

  return {
    awaiting,
    pendingLogout,
    setPendingLogout,
    onOpenLogin,
    onProbeAll,
    confirmLogout,
    busy: probeDesktop.isPending || openLogin.isPending || logoutDesktop.isPending,
  }
}
