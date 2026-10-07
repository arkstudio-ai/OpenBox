export const LEGAL_VERSION = "2026-09-28"
export const LEGAL_DOCUMENTS = ["terms", "privacy", "ai", "disclaimer", "contact"] as const
export function legalPath(document = "", language = "zh-CN") {
  return `/legal/${language.startsWith("zh") ? "" : "en/"}${document ? `${document}/` : ""}`
}
