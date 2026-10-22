import { renderHook } from "@testing-library/react"
import { createInstance } from "i18next"
import { I18nextProvider } from "react-i18next"
import type { ReactNode } from "react"
import { describe, expect, it } from "vitest"
import en from "@/locales/en-US/errors.json"
import zh from "@/locales/zh-CN/errors.json"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "./useApiErrorMessage"

describe("stopped task refusals", () => {
  it.each(["en-US", "zh-CN"])("explains the task state instead of suggesting refresh (%s)", async (lng) => {
    const i18n = createInstance()
    await i18n.init({
      lng,
      resources: { "en-US": { errors: en }, "zh-CN": { errors: zh } },
      ns: ["errors"],
      defaultNS: "errors",
    })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
    )
    const { result, unmount } = renderHook(useApiErrorMessage, { wrapper })
    for (const code of [
      "ASSISTANT_STILL_STOPPING",
      "ASSISTANT_TASK_NOT_RUNNING",
      "ASSISTANT_TASK_HELD",
      "ASSISTANT_EFFECT_UNRESOLVED",
    ] as const) {
      const message = result.current(new ApiError(409, code, "server refusal"))
      expect(message).toBe((lng === "zh-CN" ? zh : en)[code])
      expect(message).not.toBe(i18n.t("HTTP_409"))
    }
    unmount()
  })
})
