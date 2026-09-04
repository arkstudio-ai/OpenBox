// 云桌面 tab: streams the sandbox's Wuying cloud desktop through Alibaba's
// Web SDK. The backend hands us a one-time connection ticket (202-pending
// retried here); the SDK renders the remote screen into an iframe we scale to
// fit. Read-only by default — the agent works on that desktop, so taking the
// mouse is an explicit choice. No machine ids ever reach the UI.
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Maximize2, Minimize2, RotateCw, Upload } from "lucide-react"
import { http, ApiError } from "@/shared/api/http"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

const SDK_URL =
  "https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.13.9-asp3.18.11/WuyingWebSDK/WuyingWebSDK.js"
const SDK_PATH =
  "https://g.alicdn.com/aliyun-ecs/WuyingWebSdk-multi/2.13.9-asp3.18.11/WuyingWebSDK/sdk/ASP/container.html"
const FRAME_ID = "wuying-desktop-frame"
// Keep the stream at the desktop's fixed aspect ratio. The iframe itself is
// laid out at its displayed size: CSS-transforming the iframe makes the
// SDK observe a different input coordinate space than the user clicks in.
// Per-user desktops are pinned to 16:9 1080p by the "OpenBox Personal 1080p"
// ECD policy group (WUYING_POLICY_GROUP_ID); only the shared agent desktop
// stays XGA for cheaper computer-use screenshots.
const REMOTE_W = 1920
const REMOTE_H = 1080

interface WuyingSession {
  start: () => void
  stop?: () => void
  stopConnection?: () => void
  addHandle: (event: string, cb: (data?: { code?: string | number; message?: string }) => void) => void
  enableInput?: (on: boolean) => void
  enableKeyBoard?: (on: boolean) => void
  setInputEnabled?: (on: boolean) => void
  setTouchEnabled?: (on: boolean) => void
  setMouseMode?: (mode: "Client" | "Server") => void
  /** Two-way clipboard bridge between this page and the remote desktop. */
  setClipboardEnabled?: (on: boolean) => void
  /** Sends a local file to the desktop; showDialog surfaces remote progress UI. */
  uploadFile?: (file: File, showDialog: boolean) => void
}

interface WuyingGlobal {
  WebSDK?: { createSession: (id: string, opts: Record<string, unknown>) => WuyingSession | null }
}

interface Ticket {
  ticket?: string
  desktopId?: string
  regionId?: string
  pending?: boolean
  taskId?: string
}

interface DesktopStatus {
  state: string
  mode?: string
  desktopId?: string
  error?: string
  channel?: { state: string; last_seen_at?: string | null; error?: string }
}

/** Thrown when the backend says this user's desktop failed to provision. */
class ProvisionFailedError extends Error {
  constructor(readonly detail: string) {
    super("provision_failed")
  }
}

let sdkLoading: Promise<void> | null = null
function loadSdk(): Promise<void> {
  const w = window as { Wuying?: WuyingGlobal }
  if (w.Wuying?.WebSDK) return Promise.resolve()
  sdkLoading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script")
    script.src = SDK_URL
    script.async = true
    script.onload = () => {
      if ((window as { Wuying?: WuyingGlobal }).Wuying?.WebSDK) resolve()
      else reject(new Error("sdk"))
    }
    script.onerror = () => {
      sdkLoading = null
      reject(new Error("sdk"))
    }
    document.head.appendChild(script)
  })
  return sdkLoading
}

/** Poll the ticket endpoint through its 202-pending window. */
async function fetchTicket(alive: () => boolean): Promise<Ticket> {
  let taskId = ""
  for (let attempt = 0; attempt < 30 && alive(); attempt += 1) {
    const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""
    const data = await http.get<Ticket>(`/api/desktop/ticket${query}`)
    if (data.ticket) return data
    taskId = data.taskId ?? taskId
    await new Promise((r) => setTimeout(r, 3000))
  }
  throw new Error("timeout")
}

/** Wait out a per-user desktop that is still creating/starting (2-3 min cold). */
async function waitDesktopRunning(alive: () => boolean, onProgress: (state: string) => void): Promise<void> {
  for (let attempt = 0; attempt < 120 && alive(); attempt += 1) {
    const status = await http.get<DesktopStatus>("/api/desktop/status")
    if (status.state === "running") return
    if (status.state === "failed") throw new ProvisionFailedError(status.error ?? "")
    if (status.state === "not_provisioned") throw new Error("not_provisioned")
    onProgress(status.state)
    await new Promise((r) => setTimeout(r, 5000))
  }
  throw new Error("timeout")
}

type Phase = "loading" | "connected" | "error" | "closed" | "provision" | "provisionFailed"

type Fullscreen = "off" | "native" | "fallback"

/** Prefer the current SDK input API and keep the legacy method as fallback. */
function setSessionControl(session: WuyingSession | null, on: boolean) {
  if (!session) return
  if (session.setInputEnabled) session.setInputEnabled(on)
  else session.enableInput?.(on)
  // Wuying exposes keyboard activation separately from the general input
  // gate. This also activates the SDK's hidden IME proxy used for composed
  // Chinese text; setInputEnabled alone only controls event forwarding.
  session.enableKeyBoard?.(on)
  session.setTouchEnabled?.(on)
  // Normal desktop interaction needs absolute coordinates; relative (Server)
  // mode is intended for captured-pointer workloads such as 3D applications.
  if (on) session.setMouseMode?.("Client")
}

function focusFrame(frame: HTMLIFrameElement | null) {
  if (!frame) return
  try {
    frame.focus({ preventScroll: true })
  } catch {
    // A browser may refuse cross-origin focus outside a user gesture. The next
    // pointer press inside the iframe will still focus it normally.
  }
}

function useChannelState(phase: Phase) {
  const [state, setState] = useState("")
  useEffect(() => {
    if (phase !== "connected") return
    const timer = window.setInterval(() => {
      void http
        .get<DesktopStatus>("/api/desktop/status")
        .then((status) => setState(status.channel?.state ?? ""))
        .catch(() => setState("down"))
    }, 30_000)
    return () => window.clearInterval(timer)
  }, [phase])
  return [state, setState] as const
}

function ChannelStatus({ state }: { state: string }) {
  const { t } = useTranslation("workbench")
  if (!state) return null
  return (
    <span className="flex-none rounded-full bg-hairsoft px-2 py-0.5 text-xs text-n600">
      {t(`desktop.channel.${state}`, { defaultValue: state })}
    </span>
  )
}

export function DesktopTab() {
  const { t } = useTranslation("workbench")
  const rootRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const sessionRef = useRef<WuyingSession | null>(null)
  const [phase, setPhase] = useState<Phase>("loading")
  const [detail, setDetail] = useState("")
  const [control, setControl] = useState(false)
  const [clipboard, setClipboard] = useState(true)
  const [fs, setFs] = useState<Fullscreen>("off")
  const [attempt, setAttempt] = useState(0)
  const [channelState, setChannelState] = useChannelState(phase)
  // The connect effect outlives renders; mirror the toggles for it.
  const togglesRef = useRef({ control: false, clipboard: true })
  useEffect(() => {
    togglesRef.current = { control, clipboard }
  }, [control, clipboard])

  // (Re)connect whenever `attempt` bumps; tear the session down on unmount.
  // Phase starts as "loading" and the reconnect button resets it, so the
  // effect never needs to set state synchronously.
  useEffect(() => {
    let alive = true
    const stage = stageRef.current

    const stop = () => {
      const s = sessionRef.current
      sessionRef.current = null
      frameRef.current = null
      try {
        if (s?.stop) s.stop()
        else s?.stopConnection?.()
      } catch {
        // already gone
      }
    }

    // The connection lifecycle deliberately keeps each guarded SDK phase in
    // one closure so cleanup can invalidate every continuation via `alive`.
    // eslint-disable-next-line complexity
    void (async () => {
      try {
        // Per-user mode: make sure this user's own desktop exists and is
        // Running before asking for a ticket. Shared mode reports "running"
        // whenever it is usable, so this stays a single code path. A failing
        // status endpoint (older backend, provider misconfig) falls through to
        // the ticket call, which owns the definitive error.
        const status = await http.get<DesktopStatus>("/api/desktop/status").catch(() => null)
        if (!alive) return
        if (status) {
          setChannelState(status.channel?.state ?? "")
          if (status.state === "not_provisioned" && status.mode === "per_user") {
            setPhase("provision")
            return
          }
          if (status.state === "failed") throw new ProvisionFailedError(status.error ?? "")
          if (status.state && status.state !== "running" && status.state !== "not_provisioned") {
            setDetail(t("desktop.provisioning"))
            await waitDesktopRunning(
              () => alive,
              () => setDetail(t("desktop.provisioning")),
            )
            if (!alive) return
            setDetail("")
          }
        }

        const [, ticket] = await Promise.all([loadSdk(), fetchTicket(() => alive)])
        if (!alive || !stage) return

        stage.replaceChildren()
        const frame = document.createElement("iframe")
        frame.id = FRAME_ID
        frame.title = t("desktop.frameTitle")
        frame.allow = "clipboard-read; clipboard-write; fullscreen"
        frame.allowFullscreen = true
        frame.tabIndex = 0
        frame.style.cssText = "position:absolute;display:block;border:0;"
        frameRef.current = frame
        stage.appendChild(frame)

        const sdk = (window as { Wuying?: WuyingGlobal }).Wuying?.WebSDK
        if (!sdk) throw new Error("sdk")
        const session = sdk.createSession(`bossip-desktop-${Date.now()}`, {
          openType: "inline",
          iframeId: FRAME_ID,
          sdkPath: SDK_PATH,
          resourceType: "local",
          connectType: "desktop",
          regionId: ticket.regionId,
          userInfo: { ticket: ticket.ticket },
          desktopInfo: {
            desktopId: ticket.desktopId,
            loginRegionId: ticket.regionId,
            connConfig: {
              // Let composition text from macOS/Windows IMEs reach the guest
              // instead of reducing it to physical key scan codes.
              useCustomIme: true,
              disableIME: false,
              // The agent, screenshots and Wuying policy all use XGA. Never
              // let a browser resize renegotiate the remote X11 framebuffer.
              resolutionAdaptive: false,
              enableAutoSwitchMouseMode: true,
              // Show media-resume hints without consuming the click that also
              // targets the remote desktop (1 + 2 + 8 + 16).
              mediaSuspendedTipFlag: 27,
            },
          },
          uiConfig: {
            toolbar: { visible: false },
            exitCheck: false,
            reconnectType: "simple",
            // "B" multiplies by devicePixelRatio and changes across clients.
            // The fixed server-side policy is authoritative.
            defaultResolution: "A",
          },
        })
        if (!session) throw new Error("sdk")
        sessionRef.current = session
        session.addHandle("onConnected", () => {
          if (!alive || sessionRef.current !== session) return
          setPhase("connected")
          const { control: takeOver, clipboard: clip } = togglesRef.current
          try {
            setSessionControl(session, takeOver)
            session.setClipboardEnabled?.(clip)
            if (takeOver) focusFrame(frame)
          } catch {
            // best effort — read-only by default
          }
        })
        session.addHandle("onDisConnected", () => {
          if (!alive || sessionRef.current !== session) return
          sessionRef.current = null
          setPhase("closed")
        })
        session.addHandle("onError", (err) => {
          if (!alive || sessionRef.current !== session) return
          setPhase("error")
          setDetail(String(err?.message ?? err?.code ?? ""))
        })
        session.start()
      } catch (e) {
        if (!alive) return
        if (e instanceof ProvisionFailedError) {
          setPhase("provisionFailed")
          setDetail(e.detail)
          return
        }
        if (e instanceof Error && e.message === "not_provisioned") {
          setPhase("provision")
          return
        }
        setPhase("error")
        if (e instanceof ApiError) setDetail(t("desktop.unavailable"))
        else if (e instanceof Error && e.message === "sdk") setDetail(t("desktop.sdkFailed"))
      }
    })()

    return () => {
      alive = false
      stop()
    }
  }, [attempt, t, setChannelState])

  // Fit the iframe itself to the stage. Avoid transform: the Web SDK measures
  // its viewport to map mouse coordinates and synthesize IME input.
  useEffect(() => {
    const stage = stageRef.current
    if (!stage || typeof ResizeObserver === "undefined") return
    const apply = () => {
      const frame = frameRef.current
      if (!frame || !stage.clientWidth || !stage.clientHeight) return
      const scale = Math.min(stage.clientWidth / REMOTE_W, stage.clientHeight / REMOTE_H)
      const width = Math.max(1, Math.floor(REMOTE_W * scale))
      const height = Math.max(1, Math.floor(REMOTE_H * scale))
      frame.style.width = `${width}px`
      frame.style.height = `${height}px`
      frame.style.left = `${Math.floor((stage.clientWidth - width) / 2)}px`
      frame.style.top = `${Math.floor((stage.clientHeight - height) / 2)}px`
    }
    const ro = new ResizeObserver(apply)
    ro.observe(stage)
    const mo = new MutationObserver(apply)
    mo.observe(stage, { childList: true })
    return () => {
      ro.disconnect()
      mo.disconnect()
    }
  }, [])

  // Leaving native fullscreen (Esc or the button) must reset our state; a
  // fallback overlay handles Esc itself.
  useEffect(() => {
    const onChange = () => {
      if (!document.fullscreenElement) setFs((v) => (v === "native" ? "off" : v))
    }
    document.addEventListener("fullscreenchange", onChange)
    return () => document.removeEventListener("fullscreenchange", onChange)
  }, [])
  useEffect(() => {
    if (fs !== "fallback") return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFs("off")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [fs])

  const toggleControl = (on: boolean) => {
    setControl(on)
    const s = sessionRef.current
    try {
      setSessionControl(s, on)
      if (on) focusFrame(frameRef.current)
    } catch {
      // session mid-teardown
    }
  }

  const toggleClipboard = (on: boolean) => {
    setClipboard(on)
    try {
      sessionRef.current?.setClipboardEnabled?.(on)
    } catch {
      // session mid-teardown
    }
  }

  const toggleFullscreen = () => {
    if (fs === "native") {
      void document.exitFullscreen().catch(() => setFs("off"))
      return
    }
    if (fs === "fallback") {
      setFs("off")
      return
    }
    const root = rootRef.current
    if (!root) return
    root
      .requestFullscreen()
      .then(() => setFs("native"))
      // Browsers without the API (or that refuse it) get a fixed overlay.
      .catch(() => setFs("fallback"))
  }

  const provisionNow = async () => {
    setPhase("loading")
    setDetail(t("desktop.provisioning"))
    try {
      await http.post("/api/desktop/provision")
      setAttempt((n) => n + 1)
    } catch {
      setPhase("error")
      setDetail(t("desktop.unavailable"))
    }
  }

  const onPickFile = () => {
    const file = fileRef.current?.files?.[0]
    if (fileRef.current) fileRef.current.value = ""
    if (!file) return
    try {
      // showDialog=true: the remote desktop surfaces its own progress UI.
      sessionRef.current?.uploadFile?.(file, true)
    } catch {
      // session mid-teardown
    }
  }

  return (
    <div
      ref={rootRef}
      className={cn(
        "flex min-h-0 flex-1 flex-col",
        fs === "fallback" ? "fixed inset-0 z-50 bg-bg p-3" : "px-3 pb-3",
        fs === "native" && "bg-bg p-3",
      )}
    >
      <div className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1.5 pb-2.5">
        <span
          className={cn(
            "size-2 flex-none rounded-full",
            phase === "connected" ? "bg-s500" : phase === "loading" ? "bg-a400" : "bg-n400",
          )}
          aria-hidden
        />
        <span className="min-w-0 flex-1 truncate text-sm text-n700">
          {phase === "connected"
            ? control
              ? t("desktop.controlOn")
              : t("desktop.readonly")
            : t(`desktop.${phase}`)}
        </span>
        <ChannelStatus state={channelState} />
        {phase === "connected" && (
          <>
            <label className="flex flex-none cursor-pointer items-center gap-1.5 text-sm text-n700">
              <input
                type="checkbox"
                checked={control}
                onChange={(e) => toggleControl(e.target.checked)}
                className="accent-a700"
              />
              {t("desktop.allowControl")}
            </label>
            <label className="flex flex-none cursor-pointer items-center gap-1.5 text-sm text-n700">
              <input
                type="checkbox"
                checked={clipboard}
                onChange={(e) => toggleClipboard(e.target.checked)}
                className="accent-a700"
              />
              {t("desktop.clipboard")}
            </label>
            {control && (
              <span className="flex-none text-xs text-n500" title={t("desktop.imeHintDetail")}>
                {t("desktop.imeHint")}
              </span>
            )}
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              title={t("desktop.upload")}
              aria-label={t("desktop.upload")}
              className="flex size-7 flex-none items-center justify-center rounded-full text-n700 hover:bg-hairsoft"
            >
              <Upload size={14.5} strokeWidth={2.2} />
            </button>
          </>
        )}
        <button
          type="button"
          onClick={toggleFullscreen}
          title={fs === "off" ? t("desktop.fullscreen") : t("desktop.exitFullscreen")}
          aria-label={fs === "off" ? t("desktop.fullscreen") : t("desktop.exitFullscreen")}
          className="flex size-7 flex-none items-center justify-center rounded-full text-n700 hover:bg-hairsoft"
        >
          {fs === "off" ? <Maximize2 size={14.5} strokeWidth={2.2} /> : <Minimize2 size={14.5} strokeWidth={2.2} />}
        </button>
        {(phase === "error" || phase === "closed") && (
          <RetryButton
            label={t("desktop.reconnect")}
            onClick={() => {
              setPhase("loading")
              setDetail("")
              setAttempt((n) => n + 1)
            }}
          />
        )}
        {phase === "provisionFailed" && (
          <RetryButton label={t("desktop.provisionRetry")} onClick={() => void provisionNow()} />
        )}
        <input ref={fileRef} type="file" onChange={onPickFile} className="hidden" aria-hidden tabIndex={-1} />
      </div>

      <div className="relative min-h-0 flex-1 overflow-hidden rounded-2xl border border-hair bg-card">
        <div ref={stageRef} className="absolute inset-0" data-testid="desktop-stage" />
        {phase !== "connected" && <StageOverlay phase={phase} detail={detail} onProvision={() => void provisionNow()} />}
      </div>
    </div>
  )
}

function RetryButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-none items-center gap-1.5 rounded-full border border-hair px-3 py-1 text-sm text-n800 hover:bg-hairsoft"
    >
      <RotateCw size={13} strokeWidth={2.4} />
      {label}
    </button>
  )
}

// The full-stage message shown while the desktop is not streaming: spinner,
// first-time provisioning opt-in, or the failure copy.
function StageOverlay({
  phase,
  detail,
  onProvision,
}: {
  phase: Exclude<Phase, "connected">
  detail: string
  onProvision: () => void
}) {
  const { t } = useTranslation("workbench")
  const canProvision = useWorkspaceStore((state) => {
    const selected = state.items.find((item) => item.id === state.currentId)
    return selected?.role === "owner" || selected?.role === "admin"
  })
  return (
    <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-card">
      {phase === "loading" ? (
        <>
          <Spinner className="size-5" />
          <span className="text-sm text-n600">{detail || t("desktop.loading")}</span>
        </>
      ) : phase === "provision" ? (
        <>
          <span className="text-base text-n800">{t("desktop.provision")}</span>
          <span className="max-w-90 text-center text-sm text-n600">{t("desktop.provisionHint")}</span>
          {canProvision ? (
            <button
              type="button"
              onClick={onProvision}
              className="rounded-full bg-ink px-4 py-1.5 text-sm text-bg hover:bg-a800"
            >
              {t("desktop.provisionAction")}
            </button>
          ) : (
            <span className="text-sm text-n600">{t("desktop.provisionRestricted")}</span>
          )}
        </>
      ) : (
        <>
          <span className="text-base text-n800">{t(`desktop.${phase}`)}</span>
          {detail && <span className="max-w-90 text-center text-sm text-n600">{detail}</span>}
        </>
      )}
    </div>
  )
}
