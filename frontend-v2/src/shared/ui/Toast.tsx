// Toast host — one of the ≤3 global stores (auth, appearance, toast).
//
// Shaped as a card rather than a pill because the messages that matter most are
// the long ones: a quota refusal runs to a couple of sentences, and a pill with
// no max width stretched it into a single unreadable line that then vanished
// before it could be read. Cards wrap, carry an icon so the kind is legible at
// a glance, stay long enough to finish reading, and can be dismissed early.
import { useEffect } from "react"
import { create } from "zustand"
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

export type ToastKind = "info" | "success" | "warning" | "error"

interface ToastItem {
  id: number
  kind: ToastKind
  text: string
  /** Optional heading above the message, for when the kind alone is not enough. */
  title?: string
  duration: number
}

export interface ToastOptions {
  title?: string
  /** Milliseconds on screen. 0 keeps it until dismissed. */
  duration?: number
}

interface ToastState {
  items: ToastItem[]
  push: (kind: ToastKind, text: string, opts?: ToastOptions) => number
  remove: (id: number) => void
  clear: () => void
}

let seq = 0

/** Reading time, floored and capped.
 *
 * A fixed 3.2s suited "Saved" and lost every sentence longer than that. Roughly
 * 14 characters a second is a slow, distracted read — the right pace to assume
 * for something that appeared without being asked for.
 */
function readingTime(text: string, title?: string): number {
  const chars = text.length + (title?.length ?? 0)
  return Math.min(12_000, Math.max(3_600, Math.round((chars / 14) * 1000) + 1_600))
}

/** Errors are worth keeping on screen; a duplicate of one is not. */
export const useToastStore = create<ToastState>((set, get) => ({
  items: [],
  push: (kind, text, opts) => {
    const existing = get().items.find((t) => t.text === text && t.kind === kind)
    if (existing) return existing.id

    const id = ++seq
    const duration = opts?.duration ?? readingTime(text, opts?.title)
    set((s) => ({ items: [...s.items, { id, kind, text, title: opts?.title, duration }] }))
    return id
  },
  remove: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
  clear: () => set({ items: [] }),
}))

export function toast(kind: ToastKind, text: string, opts?: ToastOptions): number {
  return useToastStore.getState().push(kind, text, opts)
}

/** Named helpers, so call sites read as intent rather than configuration. */
toast.info = (text: string, opts?: ToastOptions) => toast("info", text, opts)
toast.success = (text: string, opts?: ToastOptions) => toast("success", text, opts)
toast.warning = (text: string, opts?: ToastOptions) => toast("warning", text, opts)
toast.error = (text: string, opts?: ToastOptions) => toast("error", text, opts)
toast.dismiss = (id: number) => useToastStore.getState().remove(id)

const ICONS = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
} as const

const ICON_TONE = {
  info: "text-n700",
  success: "text-sage",
  warning: "text-n800",
  error: "text-danger",
} as const

function Toast({ item, onDismiss }: { item: ToastItem; onDismiss: () => void }) {
  const { t } = useTranslation("common")
  const Icon = ICONS[item.kind]

  // Timer lives with the toast so it starts when it appears and is cleaned up
  // when it leaves — a duration of 0 simply never schedules one.
  useEffect(() => {
    if (!item.duration) return
    const timer = window.setTimeout(onDismiss, item.duration)
    return () => window.clearTimeout(timer)
  }, [item.duration, onDismiss])

  return (
    <div
      role={item.kind === "error" ? "alert" : "status"}
      aria-live={item.kind === "error" ? "assertive" : "polite"}
      className={cn(
        "pointer-events-auto animate-fade-down w-full overflow-hidden rounded-2xl border border-hair",
        "bg-card shadow-pop",
      )}
    >
      <div className="flex items-start gap-2.5 px-3.5 py-3">
        <Icon size={16} strokeWidth={2} className={cn("mt-0.5 flex-none", ICON_TONE[item.kind])} aria-hidden />
        <div className="min-w-0 flex-1">
          {item.title && (
            <p className="mb-0.5 text-sm font-medium text-ink">{item.title}</p>
          )}
          {/* break-words so a long URL or id cannot push the card wider than
              the screen it is sitting on. */}
          <p className="break-words text-sm leading-6 text-ink">{item.text}</p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          aria-label={t("close")}
          className="-me-1 mt-0.5 flex size-6 flex-none items-center justify-center rounded-lg text-n600 transition-colors hover:bg-hairsoft hover:text-ink"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  )
}

export function ToastHost() {
  const items = useToastStore((s) => s.items)
  const remove = useToastStore((s) => s.remove)

  return (
    <div
      className={cn(
        "pointer-events-none fixed z-60 flex flex-col items-center gap-2",
        // Top, not bottom: the composer and its send button live along the
        // bottom edge, and a toast landing there covered the very control the
        // person had just pressed — including the draft it was telling them
        // had been kept.
        //
        // Narrow screens: a full-width column inset from both edges, below the
        // status bar. Wider ones: a centred column that stops growing, so a
        // long message wraps instead of spanning the display.
        "inset-x-3 top-[max(0.75rem,env(safe-area-inset-top))]",
        "sm:inset-x-0 sm:top-4 sm:mx-auto sm:max-w-[26rem]",
      )}
    >
      {items.map((item) => (
        <Toast key={item.id} item={item} onDismiss={() => remove(item.id)} />
      ))}
    </div>
  )
}
