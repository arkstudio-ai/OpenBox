import { env } from "@/shared/config/env"

export interface LogtoConfig {
  enabled: boolean
  endpoint: string
  issuer: string
  app_id: string
  redirect_uri: string
  post_logout_redirect_uri: string
  /** Logto is configured here, whatever the default way in is for this visitor. */
  sso_available?: boolean
  /** The default way in for this visitor: mainland China gets SSO, elsewhere the e-mail form. */
  preferred?: "sso" | "password"
  region?: "cn" | "intl"
  /** The deployment separates visitors by region; e-mail sign-up then needs no account name. */
  region_gate?: boolean
}

/**
 * Public OIDC settings; null means Logto is not configured or unreachable.
 * A visitor whose default is the e-mail form still gets the settings, so the
 * SSO button and the callback keep working for them.
 */
export async function getLogtoConfig(): Promise<LogtoConfig | null> {
  try {
    const resp = await fetch(`${env.apiBase}/api/auth/logto/config`)
    if (!resp.ok) return null
    const data = (await resp.json()) as LogtoConfig
    return (data.sso_available ?? data.enabled) ? data : null
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
