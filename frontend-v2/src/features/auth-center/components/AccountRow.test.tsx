import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { AccountRow } from "./AccountRow"
import type { PlatformAccount } from "../types"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string) => key
  return { ...actual, useTranslation: () => ({ t }) }
})

afterEach(cleanup)

const bound: PlatformAccount = {
  id: "pacc-1", platform: "douyin", authKind: "oauth", externalId: "openid-1234567890", unionId: null,
  nickname: "老板", avatarUrl: null, scopes: [], status: "bound", accessExpiresAt: null,
  refreshExpiresAt: "2026-11-01T00:00:00Z", estimatedExpiresAt: "2026-10-20T00:00:00Z", renewCount: 0,
  renewalsLeft: 3, lastRefreshAt: null, lastProbeAt: "2026-09-28T00:00:00Z", lastOkAt: null, lastError: null,
  boundAt: null, boundByUserId: "u",
}

function mount(account: PlatformAccount) {
  render(
    <AccountRow
      account={account}
      canManage
      busy={false}
      onProbe={() => undefined}
      onReauthorize={() => undefined}
      onUnbind={() => undefined}
    />,
  )
}

describe("AccountRow", () => {
  it("shows one expiry line and keeps the bookkeeping behind the details toggle", () => {
    mount(bound)
    expect(screen.getByText("account.estimatedExpiry")).toBeTruthy()
    expect(screen.queryByText("account.openId")).toBeNull()
    expect(screen.queryByText("account.lastProbe")).toBeNull()

    fireEvent.click(screen.getByText("account.details"))
    expect(screen.getByText("account.openId")).toBeTruthy()
    expect(screen.getByText("account.validUntil")).toBeTruthy()
    expect(screen.getByText("account.renewalsLeft")).toBeTruthy()
    expect(screen.getByText("account.lastProbe")).toBeTruthy()

    fireEvent.click(screen.getByText("account.hideDetails"))
    expect(screen.queryByText("account.openId")).toBeNull()
  })

  it("tells an expired account to scan again and offers re-authorization", () => {
    mount({ ...bound, status: "expired", lastError: "token revoked" })
    expect(screen.getByText("account.needsReauth")).toBeTruthy()
    expect(screen.queryByText("account.estimatedExpiry")).toBeNull()
    expect(screen.getByText("token revoked")).toBeTruthy()
    expect(screen.getByText("actions.reauthorize")).toBeTruthy()
  })
})
