import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { ListingDialog } from "./ListingDialog"

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces("admin-skills")
})
afterEach(cleanup)

function mount(props: Partial<Parameters<typeof ListingDialog>[0]> = {}) {
  const onConfirm = vi.fn()
  const onClose = vi.fn()
  render(
    <I18nextProvider i18n={i18n}>
      <ListingDialog
        open
        title="下架「网页调研」"
        body="下架后商店不再展示。"
        confirmLabel="下架"
        requireReason
        onClose={onClose}
        onConfirm={onConfirm}
        {...props}
      />
    </I18nextProvider>,
  )
  return { onConfirm, onClose, confirm: screen.getByRole("button", { name: props.confirmLabel ?? "下架" }) }
}

describe("ListingDialog", () => {
  it("keeps the confirm button disabled until a required reason is typed", () => {
    const { confirm, onConfirm } = mount()
    expect(confirm).toHaveProperty("disabled", true)
    expect(screen.getByText("下架与驳回必须填写原因。")).toBeDefined()

    // Whitespace is not a reason.
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "   " } })
    expect(confirm).toHaveProperty("disabled", true)

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "  抄袭了官方技能  " } })
    expect(confirm).toHaveProperty("disabled", false)
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledWith("抄袭了官方技能")
  })

  it("lets an optional note through empty", () => {
    const { confirm, onConfirm } = mount({ requireReason: false, confirmLabel: "上架" })
    expect(confirm).toHaveProperty("disabled", false)
    expect(screen.queryByText("下架与驳回必须填写原因。")).toBeNull()
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledWith("")
  })

  it("does not carry a reason over to the next row", () => {
    const { rerender } = render(
      <I18nextProvider i18n={i18n}>
        <ListingDialog
          open
          title="下架 A"
          body=""
          confirmLabel="下架"
          requireReason
          onClose={vi.fn()}
          onConfirm={vi.fn()}
        />
      </I18nextProvider>,
    )
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "旧原因" } })

    const dialog = (open: boolean) => (
      <I18nextProvider i18n={i18n}>
        <ListingDialog
          open={open}
          title="下架 B"
          body=""
          confirmLabel="下架"
          requireReason
          onClose={vi.fn()}
          onConfirm={vi.fn()}
        />
      </I18nextProvider>
    )
    rerender(dialog(false))
    rerender(dialog(true))
    expect(screen.getByRole("textbox")).toHaveProperty("value", "")
  })

  it("blocks confirmation while the write is in flight", () => {
    mount({ pending: true })
    expect(screen.getByRole("button", { name: "下架" })).toHaveProperty("disabled", true)
  })

  // The caller hands us one page-level mutation, and React Query keeps its error
  // state after it settles. So a dialog that trusts `failed` alone opens on the
  // next row already claiming an operation that has not been attempted failed.
  it("reports a failure only for an attempt made from this opening", () => {
    const dialog = (open: boolean, failed: boolean) => (
      <I18nextProvider i18n={i18n}>
        <ListingDialog
          open={open}
          title="下架 A"
          body=""
          confirmLabel="下架"
          requireReason
          failed={failed}
          onClose={vi.fn()}
          onConfirm={vi.fn()}
        />
      </I18nextProvider>
    )
    const { rerender } = render(dialog(true, false))
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "与官方技能重复" } })
    expect(screen.queryByRole("alert")).toBeNull()

    fireEvent.click(screen.getByRole("button", { name: "下架" }))
    rerender(dialog(true, true))
    expect(screen.getByRole("alert").textContent).toBe("操作失败，请稍后重试。")

    // Cancelled, then opened again on another row: the mutation is still in its
    // error state, but nothing has been attempted from here.
    rerender(dialog(false, true))
    rerender(dialog(true, true))
    expect(screen.queryByRole("alert")).toBeNull()
  })
})
