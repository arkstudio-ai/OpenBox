import { useState, type MouseEvent, type KeyboardEvent } from "react"
import { useTranslation } from "react-i18next"
import { PanelRight } from "lucide-react"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { Spinner } from "@/shared/ui/Spinner"
import { usePrivateBrowser } from "../hooks/usePrivateBrowser"
import { usePanelStore } from "../stores/panel"

const BUTTON = "rounded-lg border border-hair px-2.5 py-1.5 text-xs hover:bg-hairsoft disabled:cursor-not-allowed disabled:opacity-40"
const KEYS = ["Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Space"]
const NAVIGATION = ["back", "reload", "capture"] as const

function GivebackNotice({ result }: { result: { resumed: number; changed: number } | null }) {
  const { t } = useTranslation("workbench")
  if (!result) return null
  return <p role="status" className="text-sm">
    {t("privateBrowser.givenBack")}
    {result.resumed > 0 && <> {t("privateBrowser.resumeRequested", { count: result.resumed })}</>}
    {result.changed > 0 && <> {t("privateBrowser.taskChanged", { count: result.changed })}</>}
  </p>
}

function PrivateBrowserContent({ userId, workspaceId }: { userId: string; workspaceId: string }) {
  const { t } = useTranslation("workbench")
  const browser = usePrivateBrowser(userId, workspaceId)
  const [url, setUrl] = useState("")
  const [text, setText] = useState("")
  const [key, setKey] = useState("Enter")
  const disabled = !browser.controlled || browser.busy

  const point = (event: MouseEvent<HTMLButtonElement>, button: "left" | "right") => {
    const bounds = event.currentTarget.getBoundingClientRect()
    if (!browser.frame || bounds.width <= 0 || bounds.height <= 0) return
    const x = Math.max(0, Math.min(1023, Math.floor((event.clientX - bounds.left) / bounds.width * 1024)))
    const y = Math.max(0, Math.min(767, Math.floor((event.clientY - bounds.top) / bounds.height * 768)))
    void browser.operate("mouse", { x, y, button })
  }
  const keyboard = (event: KeyboardEvent<HTMLButtonElement>) => {
    const key = event.key === " " ? "Space" : event.key
    if (event.altKey || event.ctrlKey || event.metaKey || !KEYS.includes(key)) return
    event.preventDefault()
    void browser.operate("key", { key })
  }

  return (
    <div className="scr flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-3">
      <p className="text-n600 text-xs">{t("privateBrowser.hint")}</p>
      {!browser.loaded && !browser.error && <Spinner />}
      {browser.error && <p role="alert" className="text-sm">{t("privateBrowser.unavailable")}</p>}
      {browser.pending && <p role="status" className="text-sm">{t("privateBrowser.draining")}</p>}
      <GivebackNotice result={browser.givenBack} />
      <div className="flex flex-wrap items-center gap-2">
        {browser.loaded && !browser.resource && !browser.error && (
          <button className={BUTTON} disabled={browser.busy} onClick={() => void browser.ensure()}>{t("privateBrowser.prepare")}</button>
        )}
        {browser.resource && !browser.pending && (
          <>
            <button className={BUTTON} disabled={browser.busy || !browser.resource.can_takeover}
              onClick={() => void browser.control("takeover")}>{t("privateBrowser.takeover")}</button>
            <button className={BUTTON} disabled={browser.busy || !browser.resource.can_giveback}
              onClick={() => void browser.control("giveback")}>{t("privateBrowser.giveback")}</button>
            <button className={BUTTON}
              onClick={() => void browser.control("close")}>{t("privateBrowser.stop")}</button>
          </>
        )}
        {browser.pending && (
          <button className={BUTTON} disabled={browser.busy}
            onClick={() => void browser.control(browser.pending!.action, browser.pending!)}>{t("privateBrowser.continue")}</button>
        )}
        <button className={BUTTON} disabled={browser.busy} onClick={() => void browser.refresh()}>{t("privateBrowser.check")}</button>
        {browser.busy && <Spinner />}
      </div>
      {browser.resource && !browser.pending && <p className="text-n600 text-xs" role="status">
        {t(browser.controlled ? "privateBrowser.controlled" : "privateBrowser.viewAfterTakeover")}
      </p>}
      <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); void browser.operate("navigate", { url }) }}>
        <input aria-label={t("privateBrowser.address")} placeholder={t("privateBrowser.address")} type="url" pattern="https?://.+" maxLength={4096}
          className="border-hair bg-card min-w-0 flex-1 rounded-lg border px-2 py-1.5 text-sm"
          value={url} onChange={(event) => setUrl(event.target.value)} disabled={disabled} required />
        <button className={BUTTON} disabled={disabled || !url}>{t("privateBrowser.go")}</button>
      </form>
      <div className="flex flex-wrap gap-2">
        {NAVIGATION.map((kind) => (
          <button key={kind} className={BUTTON} disabled={disabled} onClick={() => void browser.operate(kind)}>{t(`privateBrowser.${kind}`)}</button>
        ))}
        {([-480, 480] as const).map((delta) => <button key={delta} className={BUTTON} disabled={disabled}
          onClick={() => void browser.operate("wheel", { x: 512, y: 384, delta_x: 0, delta_y: delta })}>
          {t(delta < 0 ? "privateBrowser.scrollUp" : "privateBrowser.scrollDown")}</button>)}
      </div>
      {browser.frame && browser.controlled ? (
        <button type="button" className="border-hair block w-full flex-none overflow-hidden rounded-lg border p-0"
          aria-label={t("privateBrowser.clickFrame")} disabled={disabled}
          onClick={(event) => point(event, "left")} onContextMenu={(event) => { event.preventDefault(); point(event, "right") }}
          onKeyDown={keyboard}>
          <img className="block h-auto w-full" src={`data:image/png;base64,${browser.frame.png_base64}`}
            width={1024} height={768} draggable={false} alt={t("privateBrowser.frame")} />
        </button>
      ) : <div className="border-hair text-n600 flex aspect-4/3 items-center justify-center rounded-lg border text-sm">
        {t("privateBrowser.noFrame")}</div>}
      {browser.frame && browser.controlled && <p className="text-n600 truncate text-xs">{browser.frame.observation.url}</p>}
      <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); void browser.operate("text", { text }); setText("") }}>
        <input aria-label={t("privateBrowser.text")} placeholder={t("privateBrowser.text")} maxLength={4096}
          className="border-hair bg-card min-w-0 flex-1 rounded-lg border px-2 py-1.5 text-sm"
          value={text} onChange={(event) => setText(event.target.value)} disabled={disabled} />
        <button className={BUTTON} disabled={disabled || !text}>{t("privateBrowser.send")}</button>
      </form>
      <div className="flex gap-2">
        <select aria-label={t("privateBrowser.key")} className="border-hair bg-card rounded-lg border px-2 text-sm"
          value={key} onChange={(event) => setKey(event.target.value)} disabled={disabled}>
          {KEYS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        <button className={BUTTON} disabled={disabled} onClick={() => void browser.operate("key", { key })}>{t("privateBrowser.press")}</button>
      </div>
    </div>
  )
}

export function PrivateBrowserTab() {
  const { t } = useTranslation("workbench")
  const userId = useAuthStore((state) => state.user?.id)
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const collapse = usePanelStore((state) => state.togglePanel)
  return <>
    <div className="flex h-13 flex-none items-center justify-between px-3">
      <span className="text-sm font-medium">{t("privateBrowser.title")}</span>
      <button type="button" className="text-n600 hover:bg-hairsoft rounded-lg p-2" onClick={collapse}
        title={t("panel.collapse")} aria-label={t("panel.collapse")}><PanelRight size={16} /></button>
    </div>
    {userId && workspaceId && <PrivateBrowserContent key={`${userId}:${workspaceId}`} userId={userId} workspaceId={workspaceId} />}
  </>
}
