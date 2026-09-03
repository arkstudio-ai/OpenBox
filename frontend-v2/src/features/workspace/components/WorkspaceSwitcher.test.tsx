import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"

const navigate = vi.fn()
vi.mock("react-router", () => ({ useNavigate: () => navigate }))
vi.mock("react-i18next", () => {
  const t = (key: string) => key
  return { useTranslation: () => ({ t }) }
})

afterEach(() => {
  cleanup()
  navigate.mockClear()
  useWorkspaceStore.setState({ items: [], currentId: null })
})

describe("WorkspaceSwitcher", () => {
  it("is hidden for one workspace", () => {
    useWorkspaceStore.setState({
      currentId: "ws-one",
      items: [{ id: "ws-one", name: "Mine", owner_user_id: "u1", kind: "personal", role: "owner" }],
    })
    render(<WorkspaceSwitcher />)
    expect(screen.queryByRole("combobox")).toBeNull()
  })

  it("selects another workspace when several are available", () => {
    useWorkspaceStore.setState({
      currentId: "ws-one",
      items: [
        { id: "ws-one", name: "Mine", owner_user_id: "u1", kind: "personal", role: "owner" },
        { id: "ws-team", name: "Team", owner_user_id: "u2", kind: "team", role: "member" },
      ],
    })
    render(<WorkspaceSwitcher />)
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "ws-team" } })
    expect(useWorkspaceStore.getState().currentId).toBe("ws-team")
    expect(navigate).toHaveBeenCalledWith("/app")
  })
})
