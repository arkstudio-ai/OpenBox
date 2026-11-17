import { env } from "@/shared/config/env"

export interface LogtoConfig {
  enabled: boolean
  endpoint: string
  issuer: string
  app_id: string
  redirect_uri: string
  post_logout_redirect_uri: string
}

/** Public OIDC settings; null means Logto is disabled or unreachable. */
export async function getLogtoConfig(): Promise<LogtoConfig | null> {
  try {
    const resp = await fetch(`${env.apiBase}/api/auth/logto/config`)
    if (!resp.ok) return null
    const data = (await resp.json()) as LogtoConfig
    return data.enabled ? data : null
  } catch {
    return null
  }
}

/**
 * Whether this deployment lets visitors sign themselves up. Read from the same
 * public endpoint whether or not SSO is on; a server that predates the switch,
 * or one that cannot be reached, counts as open.
 */
export async function getRegistrationEnabled(): Promise<boolean> {
  try {
    const resp = await fetch(`${env.apiBase}/api/auth/logto/config`)
    if (!resp.ok) return true
    const data = (await resp.json()) as { registration_enabled?: boolean }
    return data.registration_enabled !== false
  } catch {
    return true
  }
}
