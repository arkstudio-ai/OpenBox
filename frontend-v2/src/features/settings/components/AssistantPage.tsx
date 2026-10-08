import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { useAppearanceStore } from "@/shared/appearance/store"
import {
  ASSISTANT_LENGTHS,
  ASSISTANT_TONES,
  type AssistantProfile,
} from "@/shared/appearance/assistant-profile"
import { ApiError } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import { useForgetLearned, useLearnedStyle, type LearnedItem } from "../api/assistant"
import { Choices, Field, Switch } from "./controls"

type Editable = Pick<AssistantProfile, "name" | "address" | "tone" | "length" | "emoji" | "persona">
const TEXT_FIELDS = ["name", "address", "persona"] as const

/** What the server keeps of a typed value: names and the description are trimmed. */
function kept<K extends keyof Editable>(key: K, value: Editable[K]): Editable[K] {
  return ((TEXT_FIELDS as readonly string[]).includes(key) ? String(value).trim() : value) as Editable[K]
}

const input =
  "border-hair bg-card text-ink placeholder:text-n600 focus:border-n400 w-full max-w-md rounded-lg border px-3 text-base outline-none"

/** How the person wants their assistant: its name, what it calls them, how it talks. Kept with the
 *  account and followed by the web, the mobile app, the assistant's own replies and calls. Below
 *  it, what the assistant has learned on its own, each removable. */
export function AssistantPage() {
  const { t } = useTranslation("settings")
  const stored = useAppearanceStore((s) => s.assistant)
  const setAssistantProfile = useAppearanceStore((s) => s.setAssistantProfile)
  // Only what the person changed; everything else shows the stored profile, which arrives with the
  // account's preferences after sign-in and changes when the assistant is renamed in chat.
  const [draft, setDraft] = useState<Partial<Editable>>({})
  const [saving, setSaving] = useState(false)
  const value: Editable = { ...stored, ...draft }
  const patch = Object.fromEntries(
    (Object.keys(draft) as (keyof Editable)[])
      .map((key) => [key, kept(key, value[key])] as const)
      .filter(([key, next]) => next !== stored[key]),
  ) as Partial<Editable>
  const changed = Object.keys(patch).length > 0
  const change = (next: Partial<Editable>) => setDraft((current) => ({ ...current, ...next }))

  const save = async () => {
    setSaving(true)
    try {
      await setAssistantProfile(patch)
      setDraft({})
      toast("success", t("assistant.saved"))
    } catch (error) {
      const invalid = error instanceof ApiError && error.status === 422
      toast("error", invalid ? t("assistant.invalid") : t("assistant.saveFailed"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <form
        className="flex flex-col gap-6"
        onSubmit={(event) => {
          event.preventDefault()
          if (changed && !saving) void save()
        }}
      >
        <Field label={t("assistant.nameLabel")}>
          {(id) => (
            <input
              id={id}
              type="text"
              value={value.name}
              maxLength={20}
              onChange={(event) => change({ name: event.target.value })}
              placeholder={t("assistant.placeholder")}
              className={input + " h-10"}
            />
          )}
        </Field>
        <Field label={t("assistant.addressLabel")}>
          {(id) => (
            <input
              id={id}
              type="text"
              value={value.address}
              maxLength={20}
              onChange={(event) => change({ address: event.target.value })}
              placeholder={t("assistant.addressPlaceholder")}
              className={input + " h-10"}
            />
          )}
        </Field>
        <Choices
          label={t("assistant.toneLabel")}
          options={ASSISTANT_TONES}
          value={value.tone}
          text={(tone) => t(`assistant.tone.${tone}`)}
          onPick={(tone) => change({ tone })}
        />
        <Choices
          label={t("assistant.lengthLabel")}
          options={ASSISTANT_LENGTHS}
          value={value.length}
          text={(length) => t(`assistant.length.${length}`)}
          onPick={(length) => change({ length })}
        />
        <div className="max-w-md">
          <Switch
            on={value.emoji}
            label={t("assistant.emoji")}
            hint={t("assistant.emojiHint")}
            onToggle={() => change({ emoji: !value.emoji })}
          />
        </div>
        <Field label={t("assistant.personaLabel")} hint={t("assistant.personaHint")}>
          {(id) => (
            <textarea
              id={id}
              value={value.persona}
              maxLength={300}
              rows={3}
              onChange={(event) => change({ persona: event.target.value })}
              placeholder={t("assistant.personaPlaceholder")}
              className={input + " max-w-xl resize-y py-2 leading-relaxed"}
            />
          )}
        </Field>
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="submit"
            disabled={!changed || saving}
            className="bg-ink text-bg h-9 rounded-full px-4.5 text-sm font-medium disabled:opacity-40"
          >
            {t("assistant.save")}
          </button>
        </div>
        <p className="text-n600 text-xs text-pretty">{t("assistant.note")}</p>
      </form>
      <Learned />
    </div>
  )
}

/** What the assistant learned on its own about how the person likes to be helped. */
function Learned() {
  const { t } = useTranslation("settings")
  const { t: tc } = useTranslation("chat")
  const learned = useLearnedStyle()
  const titleId = useId()
  const data = learned.data
  if (!data) return null
  return (
    <section aria-labelledby={titleId} className="border-hair flex flex-col gap-3 border-t pt-6">
      <div>
        <h2 id={titleId} className="text-ink text-sm font-medium">
          {t("assistant.learned.title")}
        </h2>
        <p className="text-n600 mt-1 text-xs text-pretty">{t("assistant.learned.hint")}</p>
      </div>
      {data.learned.length ? (
        <ul className="border-hair bg-card divide-hair max-w-xl divide-y rounded-lg border">
          {data.learned.map((item) => (
            <LearnedRow key={item.id} item={item} />
          ))}
        </ul>
      ) : (
        <p className="text-n600 text-sm text-pretty">{t("assistant.learned.empty")}</p>
      )}
      {data.reactions.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-n600 text-xs">{t("assistant.learned.reactions")}</span>
          <div className="flex flex-wrap gap-1.5">
            {data.reactions.map(({ reason, count }) => (
              <span key={reason} className="bg-hairsoft text-n700 rounded-full px-2.5 py-1 text-xs">
                {t("assistant.learned.reaction", { reason: tc(`meta.reason.${reason}`), count })}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function LearnedRow({ item }: { item: LearnedItem }) {
  const { t } = useTranslation("settings")
  const forget = useForgetLearned()
  const textId = useId()
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <span id={textId} className="text-ink min-w-0 flex-1 text-sm leading-relaxed break-words">
        {item.summary}
      </span>
      <button
        type="button"
        aria-describedby={textId}
        disabled={forget.isPending}
        onClick={() =>
          forget.mutate(item, {
            onSuccess: () => toast("success", t("assistant.learned.removed")),
            onError: () => toast("error", t("assistant.learned.removeFailed")),
          })
        }
        className="text-n700 hover:bg-hairsoft h-7 flex-none rounded-full px-3 text-xs disabled:opacity-40"
      >
        {t("assistant.learned.remove")}
      </button>
    </li>
  )
}
