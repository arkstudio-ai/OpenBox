// Appearance = theme × mode × font-size × language, plus developer mode (the
// workbench's developer tabs) and how the person wants their assistant (its
// name, what it calls them, how it talks), which ride here because they are the
// same kind of thing: a per-person preference stored with the account. One of the three
// allowed app-global stores (ENGINEERING_SPEC §7.5). Applies data-attrs on
// <html>; persists locally at once and to server prefs when authenticated.
import { create } from "zustand"
import i18n, { persistLanguage, type AppLanguage } from "@/shared/i18n"
import { http } from "@/shared/api/http"
import type { UserPreferences } from "@/shared/types/api"
import {
  DEFAULT_ASSISTANT_PROFILE,
  readAssistantMeta,
  readAssistantProfile,
  type AssistantMeta,
  type AssistantProfile,
  type IntroStep,
} from "./assistant-profile"

/** One step of the first meeting, as POST /api/assistant/intro takes it. */
export type IntroEvent =
  | { event: "answer"; step: IntroStep; value: string }
  | { event: "skip"; step: IntroStep }
  | { event: "dismiss" | "bypass" | "nudged" }

export const THEMES = ["default", "azure", "cobalt", "graphite", "lagoon", "ink", "ochre", "sepia"] as const
export type ThemeName = (typeof THEMES)[number]
export type ColorMode = "light" | "system" | "dark"
export type FontSize = "sm" | "base" | "md" | "lg"

// Swatch pills shown on the theme cards (from the design's themeDefs).
export const THEME_META: Record<ThemeName, { temp: "warm" | "cool" | "neutral"; pills: [string, string] }> = {
  default: { temp: "warm", pills: ["#c67139", "#2c2b28"] },
  azure: { temp: "cool", pills: ["#3ba0ff", "#1c1f22"] },
  cobalt: { temp: "cool", pills: ["#1a48d0", "#191c22"] },
  graphite: { temp: "neutral", pills: ["#111111", "#3d3d3d"] },
  lagoon: { temp: "cool", pills: ["#12b39a", "#17332e"] },
  ink: { temp: "cool", pills: ["#101215", "#2c3138"] },
  ochre: { temp: "warm", pills: ["#f07c0a", "#28221a"] },
  sepia: { temp: "warm", pills: ["#8a5f52", "#2c2320"] },
}

const LOCAL_KEY = "bossip:appearance"

interface AppearanceState {
  theme: ThemeName
  mode: ColorMode
  fontSize: FontSize
  language: AppLanguage
  /** Show the review / terminal / browser / files tabs in the workbench. */
  developerMode: boolean
  /** How the person wants their assistant (assistant-profile.ts). Server-only: it belongs to the
   *  account, so it is never kept in this browser for the next person. */
  assistant: AssistantProfile
  /** Which parts the person decided and where their first meeting got to; null until read, so
   *  nothing is shown on a guess. */
  assistantMeta: AssistantMeta | null
  /** The server's whole view of the profile (fetched or pushed), applied at once. */
  applyAssistantView: (view: unknown) => void
  /** Records a step of the first meeting; an answer is saved as the person's decision. */
  recordIntro: (event: IntroEvent) => Promise<void>
  setTheme: (t: ThemeName) => void
  setMode: (m: ColorMode) => void
  setFontSize: (f: FontSize) => void
  setLanguage: (l: AppLanguage) => void
  setDeveloperMode: (on: boolean) => void
  /** Saves the fields given (the server cleans names to one line of up to 20 characters) and
   *  resolves to the whole profile it kept. */
  setAssistantProfile: (patch: Partial<AssistantProfile>) => Promise<AssistantProfile>
  hydrateFromServer: (prefs: UserPreferences) => void
}

function readLocal(): Partial<Pick<AppearanceState, "theme" | "mode" | "fontSize" | "developerMode">> {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_KEY) ?? "{}") as Partial<AppearanceState>
  } catch {
    return {}
  }
}

// Absent in jsdom: components that only read a preference must import without a document.
const media: MediaQueryList | null =
  typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null

function applyDom(theme: ThemeName, mode: ColorMode, fontSize: FontSize): void {
  const el = document.documentElement
  if (theme === "default") el.removeAttribute("data-theme")
  else el.setAttribute("data-theme", theme)
  const dark = mode === "dark" || (mode === "system" && (media?.matches ?? false))
  if (dark) el.setAttribute("data-mode", "dark")
  else el.removeAttribute("data-mode")
  if (fontSize === "base") el.removeAttribute("data-fs")
  else el.setAttribute("data-fs", fontSize)
}

function persist(
  state: Pick<AppearanceState, "theme" | "mode" | "fontSize" | "language" | "developerMode">,
): void {
  localStorage.setItem(
    LOCAL_KEY,
    JSON.stringify({
      theme: state.theme,
      mode: state.mode,
      fontSize: state.fontSize,
      developerMode: state.developerMode,
    }),
  )
  // Server prefs are best-effort: appearance must work signed-out too.
  void http
    .put("/api/auth/me/preferences", {
      theme: state.theme,
      extra: {
        mode: state.mode,
        fontSize: state.fontSize,
        locale: state.language,
        developerMode: state.developerMode,
      },
    })
    .catch(() => undefined)
}

export const useAppearanceStore = create<AppearanceState>((set, get) => {
  const local = readLocal()
  const initial = {
    theme: (THEMES as readonly string[]).includes(local.theme ?? "") ? (local.theme as ThemeName) : "default",
    mode: (["light", "system", "dark"] as const).includes(local.mode as ColorMode)
      ? (local.mode as ColorMode)
      : "system",
    fontSize: (["sm", "base", "md", "lg"] as const).includes(local.fontSize as FontSize)
      ? (local.fontSize as FontSize)
      : "base",
    language: (i18n.language === "en-US" ? "en-US" : "zh-CN") as AppLanguage,
    developerMode: local.developerMode === true,
    assistant: DEFAULT_ASSISTANT_PROFILE,
    assistantMeta: null,
  }
  applyDom(initial.theme, initial.mode, initial.fontSize)
  media?.addEventListener("change", () => {
    const s = get()
    applyDom(s.theme, s.mode, s.fontSize)
  })

  const commit = (patch: Partial<AppearanceState>) => {
    set(patch)
    const s = get()
    applyDom(s.theme, s.mode, s.fontSize)
    persist(s)
  }

  return {
    ...initial,
    setTheme: (theme) => commit({ theme }),
    setMode: (mode) => commit({ mode }),
    setFontSize: (fontSize) => commit({ fontSize }),
    setDeveloperMode: (developerMode) => {
      set({ developerMode })
      persist(get())
    },
    applyAssistantView: (view) => {
      const value = (view && typeof view === "object" ? view : {}) as Record<string, unknown>
      set({
        assistant: readAssistantProfile(value),
        assistantMeta: readAssistantMeta(value.decided, value.intro),
      })
    },
    setAssistantProfile: async (patch) => {
      get().applyAssistantView(await http.put<unknown>("/api/assistant/profile", patch))
      return get().assistant
    },
    recordIntro: async (event) => {
      get().applyAssistantView(await http.post<unknown>("/api/assistant/intro", event))
    },
    setLanguage: (language) => {
      set({ language })
      void i18n.changeLanguage(language)
      persistLanguage(language)
      const s = get()
      persist(s)
    },
    hydrateFromServer: (prefs) => {
      const extra = (prefs.extra ?? {}) as Record<string, unknown>
      const patch: Partial<AppearanceState> = {}
      if (typeof prefs.theme === "string" && (THEMES as readonly string[]).includes(prefs.theme))
        patch.theme = prefs.theme as ThemeName
      if (extra.mode === "light" || extra.mode === "system" || extra.mode === "dark") patch.mode = extra.mode
      if (
        extra.fontSize === "sm" ||
        extra.fontSize === "base" ||
        extra.fontSize === "md" ||
        extra.fontSize === "lg"
      )
        patch.fontSize = extra.fontSize
      if (typeof extra.developerMode === "boolean") patch.developerMode = extra.developerMode
      patch.assistant = readAssistantProfile(extra.assistant_profile, extra.assistant_name)
      patch.assistantMeta = readAssistantMeta(extra.assistant_decided, extra.assistant_intro)
      set(patch)
      const s = get()
      applyDom(s.theme, s.mode, s.fontSize)
      if (extra.locale === "zh-CN" || extra.locale === "en-US") {
        if (extra.locale !== s.language) {
          set({ language: extra.locale })
          void i18n.changeLanguage(extra.locale)
          persistLanguage(extra.locale)
        }
      }
      localStorage.setItem(
        LOCAL_KEY,
        JSON.stringify({
          theme: get().theme,
          mode: get().mode,
          fontSize: get().fontSize,
          developerMode: get().developerMode,
        }),
      )
    },
  }
})
