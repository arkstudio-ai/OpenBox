// How the person wants their assistant (backend assistant/profile.py): what it is called, what it
// calls them, how it talks and how calls go. One preference for the web, the phone, the
// assistant's own replies and the phone front desk; GET/PUT /api/assistant/profile.
export const ASSISTANT_TONES = ["warm", "professional", "lively"] as const
export const ASSISTANT_LENGTHS = ["brief", "balanced", "detailed"] as const
export const CALL_DETAILS = ["brief", "detailed"] as const

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
  }
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
