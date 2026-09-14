import { useEffect, useRef, useState } from "react"
import type { Seq } from "../../types/protocol"
import { ltSeq } from "../../utils/seq"

/**
 * A watermark for detail reads that follows a fast-moving position at most
 * once per `delayMs` (live streaming, playback) instead of per event. It may
 * lag behind the shown position — showing an earlier state — but never runs
 * ahead: moving backwards is applied immediately.
 */
export function useSettledSeq(seq: Seq, delayMs: number): Seq {
  const [settled, setSettled] = useState(seq)
  const latest = useRef(seq)
  const behind = seq !== settled

  useEffect(() => {
    latest.current = seq
  })

  useEffect(() => {
    if (delayMs <= 0 || !behind) return
    const timer = window.setTimeout(() => setSettled(latest.current), delayMs)
    return () => window.clearTimeout(timer)
  }, [behind, delayMs, settled])

  if (delayMs <= 0) return seq
  return ltSeq(seq, settled) ? seq : settled
}
