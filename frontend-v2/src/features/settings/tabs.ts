export const SETTINGS_TABS = ["account", "usage", "models", "browser", "appearance"] as const
export type SettingsTab = (typeof SETTINGS_TABS)[number]
