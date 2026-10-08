export const SETTINGS_TABS = [
  "account",
  "team",
  "models",
  "assistant",
  "voice",
  "browser",
  "publish",
  "appearance",
] as const
export type SettingsTab = (typeof SETTINGS_TABS)[number]
