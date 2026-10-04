import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useAuthStore } from "@/shared/api/auth-store"
import { planInboxLink, useOpenInboxLink } from "./resolveLink"

const navigate = vi.fn()
vi.mock("react-router", () => ({ useNavigate: () => navigate }))

const MEMBER_OF = ["home", "team"]

afterEach(() => {
  vi.restoreAllMocks()
  navigate.mockClear()
  useWorkspaceStore.setState({ items: [], currentId: null })
})

describe("planInboxLink", () => {
  it("links a specific assistant task and original result", () => {
    expect(planInboxLink({ kind: "assistant_task", workspaceId: "team", sessionId: "main", taskId: "t1", resultId: "r1" }, MEMBER_OF))
      .toMatchObject({ to: "/app/assistant?task=t1&result=r1", taskId: "t1", resultId: "r1" })
    expect(planInboxLink({ kind: "assistant_task", workspaceId: "elsewhere", taskId: "t1", resultId: "r1", sessionId: "main" }, MEMBER_OF))
      .toEqual({ action: "unavailable" })
  })
  it("routes session, cron, auth centre and skills inside a workspace the user belongs to", () => {
    expect(planInboxLink({ kind: "session", workspaceId: "team", sessionId: "s1" }, MEMBER_OF)).toEqual({
      action: "navigate",
      to: "/app/s/s1",
      workspaceId: "team",
      sessionId: "s1",
    })
    expect(
      planInboxLink(
        { kind: "session", workspaceId: "home", sessionId: "s2", panel: "desktop", control: true },
        MEMBER_OF,
      ),
    ).toMatchObject({ to: "/app/s/s2?panel=desktop&control=1" })
    expect(planInboxLink({ kind: "cron", workspaceId: "home" }, MEMBER_OF)).toMatchObject({ to: "/app/cron" })
    expect(planInboxLink({ kind: "auth_center", workspaceId: "home", jobId: "j1" }, MEMBER_OF)).toMatchObject(
      {
        to: "/app/auth-center?job=j1",
      },
    )
    expect(planInboxLink({ kind: "skills", workspaceId: "home" }, MEMBER_OF)).toMatchObject({
      to: "/app/skills",
    })
  })

  it("refuses workspaces the user left and sessions without an id", () => {
    expect(planInboxLink({ kind: "cron", workspaceId: "elsewhere" }, MEMBER_OF)).toEqual({
      action: "unavailable",
    })
    expect(planInboxLink({ kind: "session", workspaceId: "home", sessionId: "" }, MEMBER_OF)).toEqual({
      action: "unavailable",
    })
  })

  it("opens topics in-app, https urls externally, and everything else in the inbox", () => {
    expect(planInboxLink({ kind: "topic", slug: "release-2026-09" }, MEMBER_OF)).toEqual({
      action: "navigate",
      to: "/topics/release-2026-09",
    })
    expect(planInboxLink({ kind: "topic", slug: "Bad Slug" }, MEMBER_OF)).toEqual({ action: "unavailable" })
    expect(planInboxLink({ kind: "url", url: "https://bossip.example/x" }, MEMBER_OF)).toEqual({
      action: "external",
      url: "https://bossip.example/x",
    })
    expect(planInboxLink({ kind: "url", url: "javascript:alert(1)" }, MEMBER_OF)).toEqual({
      action: "unavailable",
    })
    expect(planInboxLink({ kind: "open_app" }, MEMBER_OF)).toEqual({ action: "navigate", to: "/app/inbox" })
    expect(planInboxLink(null, MEMBER_OF)).toEqual({ action: "navigate", to: "/app/inbox" })
    expect(planInboxLink({ kind: "admin_skills" }, MEMBER_OF)).toMatchObject({ to: "/app/admin/skills" })
  })
})

describe("useOpenInboxLink", () => {
  const items = [
    { id: "home", name: "Home", owner_user_id: "u", kind: "personal" as const, role: "owner" as const },
    { id: "team", name: "Team", owner_user_id: "b", kind: "team" as const, role: "member" as const },
  ]

  it("checks the result source and task identity before navigating", async () => {
    useWorkspaceStore.setState({ items, currentId: "home" })
    const get = vi.spyOn(http, "get").mockResolvedValue({ task: { task: { id: "t1" } } })
    const { result } = renderHook(() => useOpenInboxLink())
    const link = { kind: "assistant_task" as const, workspaceId: "team", taskId: "t1", resultId: "r1", sessionId: "main" }
    await act(async () => { expect(await result.current(link)).toBe("opened") })
    expect(get).toHaveBeenCalledWith("/api/assistant/results/r1/target", { headers: { "X-Workspace-Id": "team" } })
    expect(navigate).toHaveBeenCalledWith("/app/assistant?task=t1&result=r1")
    navigate.mockClear()
    get.mockResolvedValue({ task: { task: { id: "another-task" } } })
    await act(async () => { expect(await result.current(link)).toBe("unavailable") })
    expect(navigate).not.toHaveBeenCalled()
  })

  it("does not switch workspace after an account change during verification", async () => {
    useWorkspaceStore.setState({ items, currentId: "home" })
    const oldUser = useAuthStore.getState().user
    vi.spyOn(http, "get").mockImplementation(async () => {
      useAuthStore.setState({ user: { ...oldUser, id: "new-owner" } as NonNullable<typeof oldUser> })
      return { task: { task: { id: "t1" } } }
    })
    const { result } = renderHook(() => useOpenInboxLink())
    await act(async () => { expect(await result.current({ kind: "assistant_task", workspaceId: "team", taskId: "t1", resultId: "r1", sessionId: "main" })).toBe("unavailable") })
    expect(navigate).not.toHaveBeenCalled()
    expect(useWorkspaceStore.getState().currentId).toBe("home")
    useAuthStore.setState({ user: oldUser })
  })

  it("verifies session access in the target workspace, switches scope, then navigates", async () => {
    useWorkspaceStore.setState({ items, currentId: "home" })
    const get = vi.spyOn(http, "get").mockResolvedValue({})
    const { result } = renderHook(() => useOpenInboxLink())
    let outcome = ""
    await act(async () => {
      outcome = await result.current({ kind: "session", workspaceId: "team", sessionId: "s1" })
    })
    expect(outcome).toBe("opened")
    expect(get).toHaveBeenCalledWith("/api/agent/session/s1", { headers: { "X-Workspace-Id": "team" } })
    expect(useWorkspaceStore.getState().currentId).toBe("team")
    expect(navigate).toHaveBeenCalledWith("/app/s/s1")
  })

  it("reports a session the user can no longer see without switching workspace", async () => {
    useWorkspaceStore.setState({ items, currentId: "home" })
    vi.spyOn(http, "get").mockRejectedValue(new Error("404"))
    const { result } = renderHook(() => useOpenInboxLink())
    let outcome = ""
    await act(async () => {
      outcome = await result.current({ kind: "session", workspaceId: "team", sessionId: "gone" })
    })
    expect(outcome).toBe("unavailable")
    expect(useWorkspaceStore.getState().currentId).toBe("home")
    expect(navigate).not.toHaveBeenCalled()
  })

  it("opens https links in a new tab and never navigates in-app for them", async () => {
    useWorkspaceStore.setState({ items, currentId: "home" })
    const open = vi.spyOn(window, "open").mockImplementation(() => null)
    const { result } = renderHook(() => useOpenInboxLink())
    await act(async () => {
      await result.current({ kind: "url", url: "https://bossip.example/promo" })
    })
    expect(open).toHaveBeenCalledWith("https://bossip.example/promo", "_blank", "noopener,noreferrer")
    expect(navigate).not.toHaveBeenCalled()
  })
})
