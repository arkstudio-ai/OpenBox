export const SETTINGS_TABS = ["account", "team", "models", "browser", "publish", "appearance"] as const
export type SettingsTab = (typeof SETTINGS_TABS)[number]
