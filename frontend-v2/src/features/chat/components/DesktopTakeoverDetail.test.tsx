// The takeover card: a link into the desktop when the page is there, plain
// words when it is in the user's own browser, nothing for other questions.
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { onAppEvent } from "@/shared/events/bus"
import type { QuestionItem } from "@/shared/types/api"
import { DesktopTakeoverDetail, readTakeoverDetail, takeoverReasonKey } from "./DesktopTakeoverDetail"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

function item(detail: Record<string, unknown> | null): QuestionItem {
  return { question: "请完成验证", detail }
}

const local = {
  kind: "desktop_takeover",
  reason: "captcha_slider",
  url: "https://login.taobao.com/member/login.jhtml",
  host: "login.taobao.com",
  page: "login",
  instructions: "拖动滑块",
  browser: "local",
}

afterEach(cleanup)

describe("reading a takeover detail", () => {
  it("accepts the tool's detail and defaults what it cannot read", () => {
    expect(readTakeoverDetail({ kind: "desktop_takeover" })).toEqual({
      reason: "other",
      url: "",
      host: "",
      page: "",
      instructions: "",
      browser: "local",
    })
  })

  it("ignores every other kind of question detail", () => {
    expect(readTakeoverDetail({ kind: "video_script_approval" })).toBeNull()
    expect(readTakeoverDetail(null)).toBeNull()
    expect(readTakeoverDetail("desktop_takeover")).toBeNull()
  })

  it("maps unknown reasons to the generic label", () => {
    expect(takeoverReasonKey("captcha_click")).toBe("takeover.reason.captcha_click")
    expect(takeoverReasonKey("something-new")).toBe("takeover.reason.other")
  })
})

describe("DesktopTakeoverDetail", () => {
  it("renders nothing for a question that is not a takeover", () => {
    const { container } = render(<DesktopTakeoverDetail item={item({ kind: "memory_proposal" })} sessionId="s1" />)
    expect(container.innerHTML).toBe("")
  })

  it("links into the desktop with control on when the page is on the cloud desktop", () => {
    const seen: unknown[] = []
    const off = onAppEvent("workbench.open", (data) => seen.push(data))
    render(<DesktopTakeoverDetail item={item(local)} sessionId="s1" />)

    const link = screen.getByRole("link", { name: /takeover.open/ })
    expect(link.getAttribute("href")).toBe("/app/s/s1?panel=desktop&control=1")
    expect(screen.getByText("login.taobao.com")).toBeTruthy()
    expect(screen.getByText("takeover.reason.captcha_slider")).toBeTruthy()

    fireEvent.click(link)
    expect(seen).toEqual([{ kind: "desktop", control: true }])
    off()
  })

  it("only tells the user where to look when the page is in their own browser", () => {
    render(<DesktopTakeoverDetail item={item({ ...local, browser: "extension" })} sessionId="s1" />)
    expect(screen.queryByRole("link")).toBeNull()
    expect(screen.getByText("takeover.ownBrowser")).toBeTruthy()
  })
})
