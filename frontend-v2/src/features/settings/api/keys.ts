// Query keys carry the user id so an account switch can't serve stale data
// (ENGINEERING_SPEC §7.2). "sessions" intentionally matches the workspace key
// so the usage page reuses that cache instead of refetching.
import { assistantPreferenceKeys } from "@/shared/api/assistant-preference-keys"

export const settingsKeys = {
  config: (userId: string) => ["agent-config", userId] as const,
  agents: (userId: string) => ["agents", userId] as const,
  skills: (userId: string) => ["skills", userId] as const,
  mcp: (userId: string) => ["mcp", userId] as const,
  prefs: (userId: string) => ["prefs", userId] as const,
  sessions: (userId: string) => ["sessions", userId] as const,
  browser: (userId: string) => ["browser-status", userId] as const,
  publish: (userId: string) => ["publish-route", userId] as const,
  voices: assistantPreferenceKeys.voices,
  assistantLearned: (userId: string) => ["assistant-learned", userId] as const,
}
