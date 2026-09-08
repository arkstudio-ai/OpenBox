import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { DesktopLoginCard } from "./DesktopLoginCard"
import type { Platform, PlatformAccount } from "../types"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.name === "string" ? `${key}:${opts.name}` : key
  return { ...actual, useTranslation: () => ({ t }) }
})

afterEach(cleanup)

const sites: Platform[] = [
  { key: "douyin_creator", display: "抖音创作者中心", kind: "desktop", group: "douyin" },
  { key: "meituan_merchant", display: "美团经营宝", kind: "desktop", group: "meituan" },
  { key: "xiaohongshu_creator", display: "小红书创作平台", kind: "desktop", group: "xhs", reconPending: true },
]

const bound: PlatformAccount = {
  id: "pacc-1", platform: "douyin_creator", authKind: "desktop_cookie", externalId: "dy1", unionId: null,
  nickname: "创作者甲", avatarUrl: null, scopes: [], status: "bound", accessExpiresAt: null, refreshExpiresAt: null,
  estimatedExpiresAt: null, renewCount: 0, renewalsLeft: 0, lastRefreshAt: null, lastProbeAt: "2026-09-08T10:00:00Z",
  lastOkAt: null, lastError: null, boundAt: null, boundByUserId: "u", desktopId: "ecd-glxi1nk433hliivri",
  siteDisplay: "抖音创作者中心", predictedExpiresAt: "2026-10-08T08:00:00Z",
  probeDetail: { cookieOk: true, display: { account_name: "芊屿店" } },
}

describe("DesktopLoginCard", () => {
  it("lists every site, shows the bound one with nickname and store, and gates actions", () => {
    const onOpenLogin = vi.fn()
    const onProbe = vi.fn()
    const onLogout = vi.fn()
    render(
      <DesktopLoginCard
        sites={sites}
        accounts={[bound]}
        canManage
        busy={false}
        awaitingSite={null}
        onOpenLogin={onOpenLogin}
        onProbe={onProbe}
        onProbeAll={() => undefined}
        onLogout={onLogout}
      />,
    )
    expect(screen.getByText("抖音创作者中心")).toBeTruthy()
    expect(screen.getByText("创作者甲")).toBeTruthy()
    expect(screen.getByText("desktop.display.account:芊屿店")).toBeTruthy()
    expect(screen.getByText("desktop.status.bound")).toBeTruthy()
    // Two sites without a row are "none" and offer 去登录; the recon-pending one is disabled.
    const loginButtons = screen.getAllByText("desktop.actions.login")
    expect(loginButtons).toHaveLength(2)
    fireEvent.click(loginButtons[0])
    expect(onOpenLogin).toHaveBeenCalledWith("meituan_merchant")
    expect((loginButtons[1] as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText("actions.probe"))
    expect(onProbe).toHaveBeenCalledWith("pacc-1")
    fireEvent.click(screen.getByLabelText("desktop.actions.logout"))
    expect(onLogout).toHaveBeenCalledWith(bound)
  })

  it("hides sign-out for members and shows the awaiting state", () => {
    render(
      <DesktopLoginCard
        sites={sites}
        accounts={[{ ...bound, status: "unknown" }]}
        canManage={false}
        busy={false}
        awaitingSite="douyin_creator"
        onOpenLogin={() => undefined}
        onProbe={() => undefined}
        onProbeAll={() => undefined}
        onLogout={() => undefined}
      />,
    )
    expect(screen.queryByLabelText("desktop.actions.logout")).toBeNull()
    expect(screen.getByText("desktop.status.awaiting")).toBeTruthy()
    expect(screen.getByText("desktop.actions.viewDesktop")).toBeTruthy()
  })
})
