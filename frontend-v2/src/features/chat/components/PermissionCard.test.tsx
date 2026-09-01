import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { PermissionCard } from "./PermissionCard"

const { mutate } = vi.hoisted(() => ({ mutate: vi.fn() }))

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock("../api/permission", () => ({
  useReplyPermission: () => ({ mutate, isPending: false }),
}))

describe("PermissionCard", () => {
  afterEach(() => {
    cleanup()
    mutate.mockClear()
  })

  it("sends the backend-native permission actions", () => {
    render(<PermissionCard request={{ id: "perm-1", session_id: "session-1", tool: "bash" }} />)

    fireEvent.click(screen.getByRole("button", { name: "permission.allow" }))
    fireEvent.click(screen.getByRole("button", { name: "permission.allowAlways" }))
    fireEvent.click(screen.getByRole("button", { name: "permission.deny" }))

    expect(mutate.mock.calls.map(([reply]) => reply.action)).toEqual(["once", "always", "reject"])
  })
})
