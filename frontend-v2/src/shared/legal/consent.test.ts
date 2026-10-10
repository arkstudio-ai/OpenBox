import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { recordLegalConsent, stageLegalConsent } from "./consent"
import { LEGAL_VERSION } from "./links"
import zh from "@/locales/zh-CN/legal.json"
import en from "@/locales/en-US/legal.json"

beforeEach(() => sessionStorage.clear())
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  sessionStorage.clear()
})

it("keeps both rendered policy versions aligned with the receipt", () => {
  expect(zh.version).toBe(LEGAL_VERSION)
  expect(en.version).toBe(LEGAL_VERSION)
})

it("requires a recent explicit choice, including on an SSO callback", async () => {
  const fetcher = vi.fn()
  vi.stubGlobal("fetch", fetcher)
  await expect(recordLegalConsent("zh-CN", "new-token")).rejects.toThrow()
  vi.useFakeTimers()
  stageLegalConsent()
  vi.advanceTimersByTime(31 * 60 * 1000)
  await expect(recordLegalConsent("zh-CN", "new-token")).rejects.toThrow()
  expect(fetcher).not.toHaveBeenCalled()
})

it("uses the newly authenticated token, preserves failed receipts for retry and clears only on success", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response("", { status: 503 }))
    .mockResolvedValueOnce(new Response("{}"))
  vi.stubGlobal("fetch", fetcher)
  stageLegalConsent()
  await expect(recordLegalConsent("en-US", "fresh-token")).rejects.toThrow()
  await recordLegalConsent("en-US", "fresh-token")
  const options = fetcher.mock.calls[1][1] as RequestInit
  expect(new Headers(options.headers).get("Authorization")).toBe("Bearer fresh-token")
  expect(JSON.parse(options.body as string)).toEqual({
    version: LEGAL_VERSION,
    accepted: true,
    language: "en-US",
    channel: "web",
  })
  await expect(recordLegalConsent("en-US", "fresh-token")).rejects.toThrow()
  expect(fetcher).toHaveBeenCalledTimes(2)
})
