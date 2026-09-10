import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import i18n from "@/shared/i18n"
import { UploadDialog } from "./UploadDialog"

beforeAll(async () => {
  await i18n.changeLanguage("zh-CN")
  await i18n.loadNamespaces(["skills", "common"])
})
afterEach(cleanup)

function mount(upload = vi.fn<(file: File, name: string) => Promise<unknown>>().mockResolvedValue({})) {
  const cancel = vi.fn()
  render(
    <I18nextProvider i18n={i18n}>
      <UploadDialog
        busy={false}
        onCancel={cancel}
        onUploadArchive={upload}
        onInstallSkill={vi.fn()}
        onAddMcp={vi.fn()}
      />
    </I18nextProvider>,
  )
  return { upload, cancel }
}

const pick = (files: File[]) =>
  fireEvent.change(screen.getByLabelText("选择多个压缩包"), { target: { files } })
const file = (name: string) => new File(["test"], name, { type: "application/zip", lastModified: 1 })

it("uploads multiple files independently and retries only a failed file with the same target", async () => {
  const upload = vi
    .fn<(file: File, name: string) => Promise<unknown>>()
    .mockResolvedValueOnce({})
    .mockRejectedValueOnce(new Error("invalid ZIP"))
    .mockResolvedValueOnce({})
  mount(upload)
  const name = screen.getByRole("textbox")
  fireEvent.change(name, { target: { value: "single-only-name" } })
  const good = file("good.zip"),
    bad = file("bad.zip")
  pick([good, bad])
  fireEvent.click(screen.getByRole("button", { name: "上传 / 重试 2 个文件" }))
  await screen.findByText("invalid ZIP")
  expect(upload.mock.calls).toEqual([
    [good, ""],
    [bad, ""],
  ])
  fireEvent.click(screen.getByRole("button", { name: "上传 / 重试 1 个文件" }))
  await waitFor(() => expect(screen.getAllByText("上传成功")).toHaveLength(2))
  expect(upload.mock.calls).toEqual([
    [good, ""],
    [bad, ""],
    [bad, ""],
  ])
})

it("keeps a single custom name across retries", async () => {
  const upload = vi
    .fn<(file: File, name: string) => Promise<unknown>>()
    .mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({})
  mount(upload)
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "custom-name" } })
  const archive = file("one.zip")
  pick([archive])
  fireEvent.click(screen.getByRole("button", { name: "上传 / 重试 1 个文件" }))
  await screen.findByText("network")
  fireEvent.click(screen.getByRole("button", { name: "上传 / 重试 1 个文件" }))
  await screen.findByText("上传成功")
  expect(upload.mock.calls).toEqual([
    [archive, "custom-name"],
    [archive, "custom-name"],
  ])
})

it("deduplicates selections and lets operators remove queued files", () => {
  mount()
  pick([file("a.zip"), file("a.zip")])
  expect(screen.getAllByText("a.zip")).toHaveLength(1)
  pick([file("a.zip"), file("b.zip")])
  expect(screen.getAllByText("a.zip")).toHaveLength(1)
  fireEvent.click(screen.getByRole("button", { name: "移除 a.zip" }))
  expect(screen.queryByText("a.zip")).toBeNull()
  expect(screen.getByRole("button", { name: "上传 / 重试 1 个文件" })).toBeDefined()
})

it("rejects too many or oversized files before calling the server", () => {
  const { upload } = mount()
  pick(Array.from({ length: 21 }, (_, i) => file(`${i}.zip`)))
  expect(screen.getByRole("alert").textContent).toContain("每批最多 20 个")
  const large = file("large.zip")
  Object.defineProperty(large, "size", { value: 33 * 1024 * 1024 })
  pick([large])
  expect(screen.getByRole("alert").textContent).toContain("单个不超过 32 MB")
  expect(upload).not.toHaveBeenCalled()
})

it("disables close, switching modes and duplicate submits until the queue settles", async () => {
  let finish!: () => void
  const upload = vi.fn().mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve
      }),
  )
  const { cancel } = mount(upload)
  pick([file("one.zip")])
  fireEvent.click(screen.getByRole("button", { name: "上传 / 重试 1 个文件" }))
  fireEvent.click(screen.getByRole("button", { name: "取消" }))
  expect(cancel).not.toHaveBeenCalled()
  expect((screen.getByRole("button", { name: "正在上传，请勿关闭…" }) as HTMLButtonElement).disabled).toBe(
    true,
  )
  finish()
  await screen.findByText("上传成功")
  fireEvent.click(screen.getByRole("button", { name: "取消" }))
  expect(cancel).toHaveBeenCalledOnce()
})
