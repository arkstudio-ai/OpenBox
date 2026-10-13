import { Suspense, useEffect, useId, useRef, useState, type ComponentType, type ReactNode } from "react"
import { ArrowUpRight, Grip, MessageCircle, Minus, Settings2 } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { CompanionAvatar } from "@/shared/appearance/CompanionAvatar"
import { DEFAULT_COMPANION, useCompanionAppearance } from "@/shared/appearance/companion"
import { useAssistantNames } from "@/shared/appearance/useAssistantNames"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useFloatingPosition } from "../hooks/useFloatingPosition"
import { useAutoDismiss, useLevelVariable, useVoiceCall } from "../hooks/useVoiceCall"
import { isLive } from "../lib/reducer"
import { CompanionCallControls } from "./CompanionCallControls"

interface SettingsContext {
  inCall: boolean
  onResetPosition: () => void
  onNavigate: () => void
}
interface Props {
  children: ReactNode
  settings: ComponentType<SettingsContext>
}
const iconButton =
  "text-n700 hover:bg-hairsoft hover:text-ink flex size-11 shrink-0 items-center justify-center rounded-xl"
const PAGES = ["chat", "settings"] as const

/** One persistent PC entry; content is composed by the app, without feature-to-feature imports. */
export function AssistantCompanion({ children, settings: Settings }: Props) {
  const { t } = useTranslation("companion")
  const userId = useAuthStore((state) => state.user?.id)
  const appearance = useCompanionAppearance().data ?? DEFAULT_COMPANION
  const name = useAssistantNames().title
  const call = useVoiceCall()
  const inCall = isLive(call.status)
  const [opened, setOpened] = useState(false)
  const [visited, setVisited] = useState(false)
  const [page, setPage] = useState<"chat" | "settings">("chat")
  const open = opened || (call.status !== "idle" && call.expanded)
  const orb = useFloatingPosition(`bossip:companion:${userId}:orb`, { width: 72, height: 96 })
  const panel = useFloatingPosition(`bossip:companion:${userId}:panel`, { width: 424, height: 660 })
  const windowRef = useRef<HTMLDivElement>(null)
  const launcherRef = useRef<HTMLButtonElement>(null)
  const avatarRef = useRef<HTMLSpanElement>(null)
  const panelId = useId()
  const chatId = useId()
  const settingsId = useId()
  const sounding = inCall && (call.phase === "speaking" || call.phase === "greeting")
  useLevelVariable(avatarRef, sounding ? "out" : inCall && !call.muted ? "mic" : null)
  useAutoDismiss(call.ended, call.dismiss)

  const close = () => {
    setOpened(false)
    call.setExpanded(false)
    launcherRef.current?.focus({ preventScroll: true })
  }
  const show = () => {
    setOpened(true)
    setVisited(true)
    setPage("chat")
    call.setExpanded(true)
  }
  useEffect(() => {
    if (open) windowRef.current?.focus({ preventScroll: true })
  }, [open])
  // Render-time derivation keeps chat drafts mounted after the first open;
  // merely displaying the entry must not create a session or a sandbox.
  const contentMounted = visited || open
  if (open && !visited) setVisited(true)

  return (
    <>
      {(appearance.enabled || inCall) && (
        <div className="fixed z-40 select-none" style={orb.style}>
          <button
            ref={launcherRef}
            type="button"
            {...orb.handlers}
            onClick={open ? close : show}
            aria-label={t(open ? "closeLabel" : "openLabel", { name })}
            aria-expanded={open}
            aria-controls={panelId}
            aria-describedby={`${panelId}-move`}
            title={t("launcherHint", { name })}
            className="group focus-visible:ring-accent focus-visible:ring-offset-bg relative flex size-18 cursor-grab touch-none items-center justify-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-offset-4 active:cursor-grabbing"
          >
            <span
              ref={avatarRef}
              className={cn(
                "inline-flex size-16 drop-shadow-lg group-hover:scale-105 motion-safe:transition-transform motion-safe:duration-150",
                inCall && "motion-safe:[transform:scale(calc(1_+_var(--level,0)*0.12))]",
              )}
            >
              <CompanionAvatar appearance={appearance} className="size-full" />
            </span>
            {inCall && (
              <span className="bg-s600 border-card absolute right-1 bottom-1 size-3.5 rounded-full border-2" />
            )}
          </button>
          <span className="bg-card/90 text-n700 text-2xs mx-auto block w-fit max-w-full truncate rounded-full px-2 py-0.5 text-center shadow-sm">
            {inCall ? t("call.active") : name}
          </span>
          <span id={`${panelId}-move`} className="sr-only">
            {t("moveHint")}
          </span>
        </div>
      )}
      {contentMounted && (
        <div
          id={panelId}
          ref={windowRef}
          role="dialog"
          aria-modal="false"
          aria-label={t("windowLabel", { name })}
          tabIndex={-1}
          style={panel.style}
          hidden={!open}
          onKeyDown={(event) => {
            if (event.key === "Escape" && !event.defaultPrevented) {
              event.stopPropagation()
              close()
            }
          }}
          className={cn(
            "companion-window bg-card text-ink border-hair shadow-pop fixed z-40 flex min-h-0 flex-col overflow-hidden rounded-3xl border outline-none",
            !open && "hidden",
          )}
        >
          <header className="border-hair flex shrink-0 items-center gap-1 border-b px-3 py-2">
            <button
              type="button"
              {...panel.handlers}
              aria-label={t("moveWindow")}
              title={t("moveHint")}
              className="group flex min-h-11 min-w-0 flex-1 cursor-grab touch-none items-center gap-2 rounded-xl px-1 text-start active:cursor-grabbing"
            >
              <CompanionAvatar appearance={appearance} className="size-10" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-semibold">{name}</span>
                <span className="text-n700 text-2xs block">{t("subtitle")}</span>
              </span>
              <Grip
                size={14}
                aria-hidden
                className="text-n500 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100"
              />
            </button>
            <Link
              to={paths.assistant}
              onClick={close}
              className={iconButton}
              aria-label={t("fullPage")}
              title={t("fullPage")}
            >
              <ArrowUpRight size={18} aria-hidden />
            </Link>
            <button
              type="button"
              onClick={close}
              className={iconButton}
              aria-label={t("minimize")}
              title={t("minimize")}
            >
              <Minus size={18} aria-hidden />
            </button>
          </header>
          <div
            role="tablist"
            aria-label={t("tabsLabel")}
            className="border-hair mx-4 flex shrink-0 gap-4 border-b"
          >
            {PAGES.map((value, index) => (
              <button
                key={value}
                type="button"
                id={value === "chat" ? `${chatId}-tab` : `${settingsId}-tab`}
                role="tab"
                aria-selected={page === value}
                aria-controls={value === "chat" ? chatId : settingsId}
                tabIndex={page === value ? 0 : -1}
                onClick={() => setPage(value)}
                onKeyDown={(event) => {
                  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
                  event.preventDefault()
                  const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index
                  setPage(next === 0 ? "chat" : "settings")
                  const buttons =
                    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("[role=tab]")
                  buttons?.[next]?.focus()
                }}
                className={cn(
                  "flex min-h-11 items-center gap-2 border-b-2 px-1 text-xs font-medium",
                  page === value ? "border-ink text-ink" : "text-n700 hover:text-ink border-transparent",
                )}
              >
                {value === "chat" ? (
                  <MessageCircle size={15} aria-hidden />
                ) : (
                  <Settings2 size={15} aria-hidden />
                )}
                {t(`tabs.${value}`)}
              </button>
            ))}
          </div>
          <div
            id={chatId}
            role="tabpanel"
            aria-labelledby={`${chatId}-tab`}
            hidden={page !== "chat"}
            className={cn("min-h-0 flex-1", page === "chat" ? "flex flex-col" : "hidden")}
          >
            <Suspense
              fallback={
                <div className="flex flex-1 items-center justify-center">
                  <Spinner />
                </div>
              }
            >
              {children}
            </Suspense>
          </div>
          {page === "settings" && (
            <div
              id={settingsId}
              role="tabpanel"
              aria-labelledby={`${settingsId}-tab`}
              className="min-h-0 flex-1"
            >
              <Suspense
                fallback={
                  <div className="flex h-full items-center justify-center">
                    <Spinner />
                  </div>
                }
              >
                <Settings
                  inCall={inCall}
                  onResetPosition={() => {
                    orb.reset()
                    panel.reset()
                  }}
                  onNavigate={close}
                />
              </Suspense>
            </div>
          )}
          <div className="border-hair shrink-0 border-t">
            <CompanionCallControls />
          </div>
        </div>
      )}
    </>
  )
}
