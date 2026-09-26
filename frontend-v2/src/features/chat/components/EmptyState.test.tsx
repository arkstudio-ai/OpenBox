// The greeting's cards: the store's own when the route hands them over, the
// locale's merchant cards otherwise.
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { useAuthStore } from "@/shared/api/auth-store"
import type { AuthUser } from "@/shared/types/api"
import { EmptyState } from "./EmptyState"

const locale = [
  { title: "给门店做一条 30 秒的新客体验短视频", hint: "到店场景" },
  { title: "把最近的评价整理成一页总结", hint: "好评亮点" },
]

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { returnObjects?: boolean }) => (key === "suggestions" && opts?.returnObjects ? locale : key),
  }),
}))

afterEach(cleanup)

useAuthStore.setState({ user: { id: "u1", username: "老板", role: "user" } as AuthUser })

describe("EmptyState", () => {
  it("shows the store's starter cards when it has some", () => {
    const picked: string[] = []
    render(
      <EmptyState
        onPick={(text) => picked.push(text)}
        starterCards={[{ title: "给招牌菜『酸笋鸭』做一条 30 秒到店短视频", hint: "到店 · 招牌菜" }]}
      />,
    )
    expect(screen.queryByText(locale[0].title)).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: /酸笋鸭/ }))
    expect(picked).toEqual(["给招牌菜『酸笋鸭』做一条 30 秒到店短视频"])
  })

  it.each([undefined, null, []])("falls back to the locale cards for %s", (starterCards) => {
    render(<EmptyState onPick={() => undefined} starterCards={starterCards} />)
    expect(screen.getAllByRole("button")).toHaveLength(locale.length)
    expect(screen.getByText(locale[1].title)).toBeTruthy()
  })
})
