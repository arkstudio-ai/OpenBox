import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { createInstance } from "i18next"
import { I18nextProvider } from "react-i18next"
import type { FilePart, MessageWithParts } from "@/shared/types/api"
import zh from "@/locales/zh-CN/chat.json"
import en from "@/locales/en-US/chat.json"
import { AttachmentGallery } from "./AttachmentGallery"
import { ResultArtifacts } from "./ResultArtifacts"
import { buildAssistantContentView } from "../lib/content-view"
import directTranscript from "../lib/__fixtures__/direct-video-messages.json"

const asset = vi.hoisted(() => ({ data: { url: "/example.png" } as { url: string } | undefined }))
vi.mock("../api/assets", () => ({ useAssetUrl: () => asset }))

const i18n = createInstance()
await i18n.init({
  lng: "zh-CN",
  resources: { "zh-CN": { chat: zh }, "en-US": { chat: en } },
  interpolation: { escapeValue: false },
})
const image: FilePart = {
  id: "image",
  type: "file",
  path: "example.png",
  asset_id: "asset",
  mime_type: "image/png",
}

beforeEach(() => {
  asset.data = { url: "/example.png" }
})
afterEach(cleanup)

it("keeps the final share_file copy labeled in its real card and video preview", () => {
  const messages = structuredClone(directTranscript) as MessageWithParts[]
  const before = JSON.stringify(messages)
  const groups = buildAssistantContentView(messages, false).resultGroups
  expect(groups.at(-1)?.parts[0].relation?.kind).toBe("shared_file")
  render(
    <I18nextProvider i18n={i18n}>
      <ResultArtifacts groups={groups} verification={null} />
    </I18nextProvider>,
  )
  // The segment collection is folded; the one visible watermark belongs to the final copy.
  expect(screen.getAllByText(i18n.t("chat:aigc.label"))).toHaveLength(1)
  const name = "segment-video_fixture.mp4"
  fireEvent.click(screen.getByRole("button", { name: i18n.t("chat:gallery.open", { name }) }))
  expect(within(screen.getByRole("dialog", { name })).getAllByText(i18n.t("chat:aigc.label"))).toHaveLength(2)
  expect(JSON.stringify(messages)).toBe(before)
})

it("does not mark an ordinary shared final video merely because it has the large layout", () => {
  render(
    <I18nextProvider i18n={i18n}>
      <AttachmentGallery
        parts={[
          {
            ...image,
            path: "final.mp4",
            mime_type: "video/mp4",
            relation: { kind: "shared_file", role: "final" },
          },
        ]}
        artifactKind="shared_file"
        hero
      />
    </I18nextProvider>,
  )
  expect(screen.queryByText(i18n.t("chat:aigc.label"))).toBeNull()
})

it.each([
  ["zh-CN", "image"],
  ["en-US", "image"],
  ["zh-CN", "video"],
  ["en-US", "video"],
])("keeps generated labels in the thumbnail and enlarged preview (%s, %s)", async (language, media) => {
  await i18n.changeLanguage(language)
  const part =
    media === "image"
      ? { ...image, relation: { kind: "generated_image" } }
      : { ...image, path: "example.mp4", mime_type: "video/mp4", relation: { kind: "video_final" } }
  render(
    <I18nextProvider i18n={i18n}>
      <AttachmentGallery parts={[part]} />
    </I18nextProvider>,
  )
  expect(screen.getByText(i18n.t("chat:aigc.label"))).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: i18n.t("chat:gallery.open", { name: part.path }) }))
  const viewer = screen.getByRole("dialog", { name: part.path })
  expect(within(viewer).getAllByText(i18n.t("chat:aigc.label"))).toHaveLength(2)
  fireEvent.keyDown(window, { key: "Escape" })
  expect(screen.queryByRole("dialog")).toBeNull()
})

it.each([undefined, "shared_file", "qr_code", "computer_screenshot"])(
  "does not claim an original or evidence attachment (%s) is generated",
  (kind) => {
    render(
      <I18nextProvider i18n={i18n}>
        <AttachmentGallery parts={[{ ...image, relation: kind ? { kind } : undefined }]} />
      </I18nextProvider>,
    )
    expect(screen.queryByText(i18n.t("chat:aigc.label"))).toBeNull()
  },
)

it("does not leave a watermark on a failed image placeholder", () => {
  render(
    <I18nextProvider i18n={i18n}>
      <AttachmentGallery parts={[{ ...image, relation: { kind: "generated_image" } }]} />
    </I18nextProvider>,
  )
  fireEvent.error(screen.getByRole("img"))
  expect(screen.getByText(i18n.t("chat:gallery.failed"))).toBeTruthy()
  expect(screen.queryByText(i18n.t("chat:aigc.label"))).toBeNull()
})
