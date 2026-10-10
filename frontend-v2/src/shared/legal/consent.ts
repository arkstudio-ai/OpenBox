import { ApiError } from "@/shared/api/http"
import { env } from "@/shared/config/env"
import { LEGAL_VERSION } from "./links"

const PENDING = "bossip:pending-legal-consent"
const MAX_AGE = 30 * 60 * 1000

/** Stage only after an explicit submit. SSO may return through another document. */
export function stageLegalConsent() {
  sessionStorage.setItem(PENDING, JSON.stringify({ version: LEGAL_VERSION, at: Date.now() }))
}

export async function recordLegalConsent(language: string, accessToken: string) {
  let pending: { version?: string; at?: number } | null = null
  try {
    pending = JSON.parse(sessionStorage.getItem(PENDING) ?? "null")
  } catch {
    /* Invalid or expired records require a new explicit choice. */
  }
  const age = typeof pending?.at === "number" ? Date.now() - pending.at : Infinity
  if (pending?.version !== LEGAL_VERSION || age < 0 || age > MAX_AGE) {
    sessionStorage.removeItem(PENDING)
    throw new ApiError(400, "LEGAL_CONSENT_REQUIRED", "Please review the current policies again.")
  }
  // Authentication is not installed in the global store yet. Send only the
  // freshly issued token, without a stale account's token or refresh retry.
  const response = await fetch(`${env.apiBase}/api/auth/me/legal-consent`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
    body: JSON.stringify({
      version: LEGAL_VERSION,
      accepted: true,
      language: language.startsWith("zh") ? "zh-CN" : "en-US",
      channel: "web",
    }),
    signal: AbortSignal.timeout(15_000),
  })
  if (!response.ok)
    throw new ApiError(response.status, "LEGAL_CONSENT_FAILED", "Unable to save policy acceptance.")
  sessionStorage.removeItem(PENDING)
}
