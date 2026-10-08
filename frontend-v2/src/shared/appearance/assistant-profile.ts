// How the person wants their assistant (backend assistant/profile.py): what it is called, what it
// calls them, how it talks, their business and how calls go. One preference for the web, the phone,
// the assistant's own replies and the phone front desk; GET/PUT /api/assistant/profile.
//
// Set or not is a decision, not a value: `decided` says which fields the person chose (choosing the
// default counts; skipping a question does not), and `intro` where their first meeting got to.
export const ASSISTANT_TONES = ["warm", "professional", "lively"] as const
export const ASSISTANT_LENGTHS = ["brief", "balanced", "detailed"] as const
export const CALL_DETAILS = ["brief", "detailed"] as const
/** Kinds of business with starter ideas; anything else is the person's own words. */
export const BUSINESSES = ["beauty", "food", "retail"] as const
/** What the first meeting asks, in order. */
export const INTRO_STEPS = ["address", "name", "length", "business"] as const
export type IntroStep = (typeof INTRO_STEPS)[number]
export type IntroStatus = "new" | "started" | "done" | "dismissed" | "bypassed"

export interface AssistantProfile {
  /** "" is the default name the UI translates (个人助理 / Personal assistant). */
  name: string
  /** What it calls the person; "" for no name. */
  address: string
  tone: (typeof ASSISTANT_TONES)[number]
  length: (typeof ASSISTANT_LENGTHS)[number]
  emoji: boolean
  /** The person's own words on how it should be, up to 300 characters. */
  persona: string
  /** A call opens by mentioning the last one. */
  call_recap: boolean
  /** A call tells finished work nobody asked about on it. */
  call_reports: boolean
  call_detail: (typeof CALL_DETAILS)[number]
  /** beauty / food / retail, or their own words; "" not told. */
  business: string
}

export interface AssistantIntro {
  status: IntroStatus
  /** Each question answered or skipped in the meeting. */
  steps: Partial<Record<IntroStep, "answered" | "skipped">>
  /** The one reminder after going straight to work was shown. */
  nudged: boolean
}

export interface AssistantMeta {
  /** Fields the person decided, and where (settings, chat, intro). */
  decided: Partial<Record<keyof AssistantProfile, { at: string | null; via: string }>>
  intro: AssistantIntro
}

export const DEFAULT_ASSISTANT_META: AssistantMeta = {
  decided: {},
  intro: { status: "new", steps: {}, nudged: false },
}

export const DEFAULT_ASSISTANT_PROFILE: AssistantProfile = {
  name: "",
  address: "",
  tone: "warm",
  length: "balanced",
  emoji: false,
  persona: "",
  call_recap: true,
  call_reports: true,
  call_detail: "brief",
  business: "",
}

const oneOf = <T extends string>(choices: readonly T[], value: unknown, fallback: T): T =>
  choices.includes(value as T) ? (value as T) : fallback

/** A stored or pushed profile as the app uses it: what still holds is kept, the rest is the default.
 *  The first version kept only the name, under ``assistant_name``. */
export function readAssistantProfile(raw: unknown, legacyName?: unknown): AssistantProfile {
  const value = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>
  const text = (key: string, fallback = "") =>
    typeof value[key] === "string" ? (value[key] as string) : fallback
  const flag = (key: string, fallback: boolean) =>
    typeof value[key] === "boolean" ? (value[key] as boolean) : fallback
  const d = DEFAULT_ASSISTANT_PROFILE
  return {
    name: text("name", typeof legacyName === "string" ? legacyName : ""),
    address: text("address"),
    tone: oneOf(ASSISTANT_TONES, value.tone, d.tone),
    length: oneOf(ASSISTANT_LENGTHS, value.length, d.length),
    emoji: flag("emoji", d.emoji),
    persona: text("persona"),
    call_recap: flag("call_recap", d.call_recap),
    call_reports: flag("call_reports", d.call_reports),
    call_detail: oneOf(CALL_DETAILS, value.call_detail, d.call_detail),
    business: text("business"),
  }
}

const INTRO_STATUSES: readonly IntroStatus[] = ["new", "started", "done", "dismissed", "bypassed"]

/** The decisions and first meeting in a pushed or fetched profile, or the stored preferences. */
export function readAssistantMeta(decidedRaw: unknown, introRaw: unknown): AssistantMeta {
  const decided: AssistantMeta["decided"] = {}
  if (decidedRaw && typeof decidedRaw === "object") {
    for (const [key, value] of Object.entries(decidedRaw as Record<string, unknown>)) {
      if (key in DEFAULT_ASSISTANT_PROFILE && value && typeof value === "object") {
        const entry = value as Record<string, unknown>
        decided[key as keyof AssistantProfile] = {
          at: typeof entry.at === "string" ? entry.at : null,
          via: typeof entry.via === "string" ? entry.via : "settings",
        }
      }
    }
  }
  const intro = (introRaw && typeof introRaw === "object" ? introRaw : {}) as Record<string, unknown>
  const steps: AssistantIntro["steps"] = {}
  if (intro.steps && typeof intro.steps === "object") {
    for (const [step, result] of Object.entries(intro.steps as Record<string, unknown>)) {
      if (
        (INTRO_STEPS as readonly string[]).includes(step) &&
        (result === "answered" || result === "skipped")
      )
        steps[step as IntroStep] = result
    }
  }
  return {
    decided,
    intro: {
      status: INTRO_STATUSES.includes(intro.status as IntroStatus) ? (intro.status as IntroStatus) : "new",
      steps,
      nudged: intro.nudged === true,
    },
  }
}

/** Which questions a meeting asks. `auto` (the first one, on the welcome page): neither decided nor
 *  answered or skipped yet. `undecided` (opened by the person later): every one still undecided.
 *  `all` ("重新认识一下" in Settings): every question, with today's values as the defaults. */
export type IntroMode = "auto" | "undecided" | "all"

export function introSteps(meta: AssistantMeta, mode: IntroMode): IntroStep[] {
  if (mode === "all") return [...INTRO_STEPS]
  return INTRO_STEPS.filter(
    (step) => !(step in meta.decided) && (mode === "undecided" || !(step in meta.intro.steps)),
  )
}

/** The first meeting shows by itself on the welcome page: not ended, and something still to ask. */
export function introStartsByItself(meta: AssistantMeta): boolean {
  return (
    (meta.intro.status === "new" || meta.intro.status === "started") && introSteps(meta, "auto").length > 0
  )
}

/** The assistant's name as each place says it: ``title`` where it is a label or heading, ``mention``
 *  inside a sentence ("由{{name}}代答", "Answered by {{name}}"); ``custom`` is "" until the person
 *  names it. See useAssistantNames for components. */
export function assistantNames(custom: string, t: (key: string) => string) {
  return {
    custom,
    title: custom || t("common:assistantName.title"),
    mention: custom || t("common:assistantName.mention"),
  }
}
