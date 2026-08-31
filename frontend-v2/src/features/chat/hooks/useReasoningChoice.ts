// The reasoning strength sent with the next prompt.
//
// OpenCode exposes model variants independently from the model picker: only
// variants declared by the active model are selectable, while "default"
// clears the conversation override. Keep picks per conversation and model so
// switching models cannot leak an unsupported effort into the next request.
import { useState } from "react"
import type { ModelInfo } from "@/shared/types/api"

interface Options {
  model?: ModelInfo
  sessionModel?: string
  sessionVariant?: string | null
  sessionKey?: string
}

const NEW_SESSION_KEY = "__new_session__"

export function useReasoningChoice({ model, sessionModel, sessionVariant, sessionKey }: Options) {
  const [picked, setPicked] = useState<Map<string, string | null>>(() => new Map())
  const variants = model?.variants ?? []
  const key = `${sessionKey ?? NEW_SESSION_KEY}\u0000${model?.id ?? ""}`
  const hasPick = picked.has(key)
  const pending = picked.get(key)
  const persisted =
    model?.id === sessionModel && sessionVariant && variants.includes(sessionVariant) ? sessionVariant : null
  const activeId =
    !model || variants.length === 0
      ? undefined
      : hasPick
        ? pending && variants.includes(pending)
          ? pending
          : null
        : persisted
  const value = (() => {
    if (!model) return undefined
    if (hasPick) return activeId ?? null
    // A model switch must not carry an effort the new family may reject. The
    // explicit null tells the server to use this model's own default.
    if (sessionModel && model.id !== sessionModel) return null
    if (model.id === sessionModel && sessionVariant && !variants.includes(sessionVariant)) return null
    // No local choice: omission preserves the session value (or lets a new
    // conversation resolve the deployment default) without pinning it.
    return undefined
  })()

  return {
    variants,
    defaultId: model?.default_variant ?? null,
    activeId,
    value,
    pick: (id: string | null) => {
      if (!model || (id !== null && !variants.includes(id))) return
      setPicked((current) => {
        const next = new Map(current)
        next.set(key, id)
        return next
      })
    },
  }
}
