import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { ArrowDown, LoaderCircle } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { isInterruptionMarker, type Turn } from "../lib/turn-view"
import { AssistantTurn, TypingRow } from "./AssistantTurn"
import { UserBubble } from "./UserBubble"
import { InterruptionDivider } from "./InterruptionDivider"

const VIRTUAL_THRESHOLD = 50
/** How close to the top, in pixels, the reader gets before older turns load. */
const LOAD_OLDER_EDGE = 600
/** Pause before asking again for an older page that failed; doubles each time. */
const OLDER_RETRY_FIRST_MS = 2_000
const OLDER_RETRY_MAX_MS = 30_000

interface Row {
  key: string
  node: ReactNode
}

/** Virtualized row list — only mounted for long histories (> 50 rows). */
function VirtualRows({ rows, scrollRef }: { rows: Row[]; scrollRef: RefObject<HTMLDivElement | null> }) {
  // eslint-disable-next-line react-hooks/incompatible-library -- tanstack virtual is the project's chosen virtualizer
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 260,
    overscan: 8,
  })
  return (
    <div style={{ position: "relative", width: "100%", height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((item) => (
        <div
          key={rows[item.index].key}
          data-index={item.index}
          ref={virtualizer.measureElement}
          className="flex flex-col pb-6"
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            transform: `translateY(${item.start}px)`,
          }}
        >
          {rows[item.index].node}
        </div>
      ))}
    </div>
  )
}

interface Props {
  turns: Turn[]
  sessionId: string
  busy: boolean
  awaitingInput?: boolean
  /** Pending cards rendered below the last turn, inside the scroll area. */
  footer?: ReactNode
  /** Abort the run; the live turn's task card offers it. */
  onStop?: () => void
  /** Set while a stalled run is retrying, so the wait can say which try. */
  retry?: { attempt: number; maxAttempts: number }
  onAtBottomChange?: (atBottom: boolean) => void
  historyScrollRef?: RefObject<HTMLDivElement | null>
  /** Older turns exist before the oldest one loaded. */
  hasMore?: boolean
  loadingOlder?: boolean
  /** Fetch the turns before the oldest one loaded. */
  onLoadOlder?: () => void
}

/** Scrolling message column: centered, auto-sticks to the bottom, back-to-bottom fab. */
export function ChatFlow({ turns, sessionId, busy, awaitingInput = false, footer, onStop, retry, onAtBottomChange, historyScrollRef, hasMore = false, loadingOlder = false, onLoadOlder }: Props) {
  const { t } = useTranslation("chat")
  const scrollRef = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)
  useEffect(() => onAtBottomChange?.(atBottom), [atBottom, onAtBottomChange])

  // Only the newest card may be edited. The list is one live thing per
  // session, so an edit made from a scrolled-up card would land on the
  // current list and then show up in a *different* card than the one the
  // user clicked. Older cards stay as the record of how the list looked.
  const lastTodoKey = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const turn = turns[i]
      if (turn.kind === "assistant" && turn.parts.some((p) => p.type === "todo")) return turn.key
    }
    return null
  }, [turns])

  const rows = useMemo<Row[]>(() => {
    const list: Row[] = turns.map((turn, i) => ({
      key: turn.key,
      node:
        turn.kind === "user" ? (
          isInterruptionMarker(turn.message) ? (
            <InterruptionDivider message={turn.message} />
          ) : (
            <UserBubble message={turn.message} />
          )
        ) : (
          <AssistantTurn
            messages={turn.messages}
            meta={turn.meta}
            sessionId={sessionId}
            streaming={busy && i === turns.length - 1}
            awaitingInput={awaitingInput && i === turns.length - 1}
            retry={busy && i === turns.length - 1 ? retry : undefined}
            onStop={onStop}
            todoEditable={turn.key === lastTodoKey}
          />
        ),
    }))
    if (busy && turns.length > 0 && turns[turns.length - 1].kind === "user") {
      list.push({ key: "typing", node: <TypingRow retry={retry} /> })
    }
    return list
  }, [turns, sessionId, busy, awaitingInput, onStop, lastTodoKey, retry])

  const atBottomRef = useRef(true)
  const viewportHeightRef = useRef(0)
  const scrollTopRef = useRef(0)

  // Older turns load as the reader nears the top. Rows arriving above them
  // would push what they are reading down the page, so the distance from the
  // bottom is taken when loading starts and restored once the rows have landed.
  const firstRowKey = rows[0]?.key
  // One request per top row: not every scroll event or render while the top
  // stays put. A failed load is asked for again by scrolling away and back, or
  // after a pause while the reader is still at the top.
  const olderRequestedForRef = useRef<string | undefined>(undefined)
  const requestOlder = useCallback(() => {
    if (!scrollRef.current || !hasMore || loadingOlder || !onLoadOlder) return
    if (olderRequestedForRef.current === firstRowKey) return
    olderRequestedForRef.current = firstRowKey
    onLoadOlder()
  }, [hasMore, loadingOlder, onLoadOlder, firstRowKey])
  const requestOlderRef = useRef(requestOlder)
  useEffect(() => {
    requestOlderRef.current = requestOlder
  }, [requestOlder])
  const olderRetryRef = useRef<{ timer?: number; delay: number }>({ delay: OLDER_RETRY_FIRST_MS })
  useEffect(() => {
    const retry = olderRetryRef.current
    return () => window.clearTimeout(retry.timer)
  }, [])
  const olderAnchorRef = useRef<{ height: number; top: number; key?: string } | null>(null)
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (loadingOlder) {
      olderAnchorRef.current = { height: el.scrollHeight, top: el.scrollTop, key: firstRowKey }
      return
    }
    const anchor = olderAnchorRef.current
    olderAnchorRef.current = null
    if (!anchor) return
    const retry = olderRetryRef.current
    window.clearTimeout(retry.timer)
    if (anchor.key === firstRowKey) {
      // Nothing landed in front: the load failed, or the view was reset
      // meanwhile. A column too short to scroll can never leave the top and
      // come back, so ask again after a pause if the reader is still there.
      retry.timer = window.setTimeout(() => {
        const current = scrollRef.current
        if (!current) return
        olderRequestedForRef.current = undefined
        const nearTop = current.scrollTop < LOAD_OLDER_EDGE
        const short = current.scrollHeight - current.clientHeight <= LOAD_OLDER_EDGE
        if (nearTop || short) requestOlderRef.current()
      }, retry.delay)
      retry.delay = Math.min(retry.delay * 2, OLDER_RETRY_MAX_MS)
      return
    }
    retry.delay = OLDER_RETRY_FIRST_MS
    if (atBottomRef.current) return
    el.scrollTop = anchor.top + (el.scrollHeight - anchor.height)
    scrollTopRef.current = el.scrollTop
  }, [firstRowKey, loadingOlder])
  // A short page can leave nothing to scroll; keep filling from above until
  // the column overflows or nothing older is left.
  useEffect(() => {
    const el = scrollRef.current
    if (el && el.scrollHeight - el.clientHeight <= LOAD_OLDER_EDGE) requestOlder()
  }, [rows, requestOlder])

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const resized = viewportHeightRef.current !== 0 && viewportHeightRef.current !== el.clientHeight
    viewportHeightRef.current = el.clientHeight
    // A multiline suggestion row can resize the viewport by more than the
    // bottom threshold. Its scroll event may precede ResizeObserver; preserve
    // the previous bottom position instead of treating this as a user scroll.
    if (resized && atBottomRef.current) {
      el.scrollTop = el.scrollHeight
      scrollTopRef.current = el.scrollTop
      return
    }
    const remaining = el.scrollHeight - el.scrollTop - el.clientHeight
    // The button has a generous threshold; auto-stick must still release as
    // soon as the reader scrolls away, including short trackpad movements.
    if (remaining <= 1) atBottomRef.current = true
    else if (el.scrollTop < scrollTopRef.current) atBottomRef.current = false
    scrollTopRef.current = el.scrollTop
    if (el.scrollTop < LOAD_OLDER_EDGE) requestOlder()
    else olderRequestedForRef.current = undefined
    setAtBottom(remaining < 60)
  }, [requestOlder])

  // Stick to the bottom as content grows. Streaming changes content height
  // between React commits (streamdown animates blocks in), so follow real
  // element growth with a ResizeObserver rather than render passes.
  const contentRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = scrollRef.current
    const content = contentRef.current
    if (!el || !content) return
    viewportHeightRef.current = el.clientHeight
    const ro = new ResizeObserver(() => {
      if (atBottomRef.current) el.scrollTop = el.scrollHeight
      scrollTopRef.current = el.scrollTop
      viewportHeightRef.current = el.clientHeight
    })
    ro.observe(content)
    ro.observe(el) // Composer suggestions may change the viewport height.
    return () => ro.disconnect()
  }, [])
  useEffect(() => {
    const el = scrollRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [rows, footer])

  // Sending your own message always jumps to the bottom, wherever you were.
  const lastUserKey = [...turns].reverse().find((tn) => tn.kind === "user")?.key
  const prevUserKey = useRef(lastUserKey)
  useEffect(() => {
    if (lastUserKey !== prevUserKey.current) {
      prevUserKey.current = lastUserKey
      const el = scrollRef.current
      if (el) {
        atBottomRef.current = true
        setAtBottom(true)
        el.scrollTop = el.scrollHeight
      }
    }
  }, [lastUserKey])

  const scrollToBottom = () => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  const virtual = rows.length > VIRTUAL_THRESHOLD

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={(element) => {
          scrollRef.current = element
          if (historyScrollRef) historyScrollRef.current = element
        }}
        onScroll={onScroll}
        className="scr h-full overflow-y-auto overscroll-contain px-3 pt-1.5 pb-2 sm:px-6.5 [overflow-anchor:none]"
      >
        <div ref={contentRef} className="mx-auto flex w-full max-w-190 flex-col gap-6 pb-4">
          {virtual ? (
            <VirtualRows rows={rows} scrollRef={scrollRef} />
          ) : (
            rows.map((r) => (
              <div key={r.key} className="flex flex-col">
                {r.node}
              </div>
            ))
          )}
          {footer}
        </div>
      </div>
      <div
        aria-hidden
        className="from-bg pointer-events-none absolute inset-x-0 top-0 h-8 bg-gradient-to-b to-transparent"
      />
      {/* Over the column rather than in it, so appearing and disappearing never
          moves the rows the reader is looking at. */}
      {loadingOlder && (
        <div role="status" aria-label={t("loadingOlder")} className="pointer-events-none absolute inset-x-0 top-2 flex justify-center">
          <LoaderCircle className="text-n600 size-4 animate-spin" />
        </div>
      )}
      <div
        aria-hidden
        className={cn(
          "from-bg pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t to-transparent transition-opacity duration-150",
          atBottom ? "opacity-0" : "opacity-100",
        )}
      />
      {/* Right rail, vertically centred — where the thumb and the eye already
          are on a long scroll, and clear of the prose column, which is capped
          and centred. Pinned to the bottom it sat directly over the newest
          message and the composer's own controls. `end-` rather than `right-`
          so it follows the writing direction. */}
      {!atBottom && (
        <button
          type="button"
          onClick={scrollToBottom}
          aria-label={t("scrollBottom")}
          className="border-hair bg-card shadow-pop hover:bg-hairsoft absolute end-4 top-1/2 -translate-y-1/2 rounded-full border p-2 transition-colors"
        >
          <ArrowDown className="text-n700 size-4" />
        </button>
      )}
    </div>
  )
}
