// Appearance changes also affect the next call's automatically matched voice.
export const assistantPreferenceKeys = {
  appearance: (userId: string | undefined) => ["assistant-appearance", userId] as const,
  voices: (userId: string | undefined) => ["assistant-voices", userId] as const,
}
