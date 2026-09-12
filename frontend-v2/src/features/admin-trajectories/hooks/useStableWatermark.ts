import { useState } from "react"
import type { Seq } from "../types/protocol"

/**
 * The watermark a paged view should read at. While `follow` is true it tracks
 * `liveSeq`; once the reader stops following (scrolled into older pages,
 * paused, selecting) it stays at the last followed value, so a moving head
 * cannot rebuild the pages — and lose the scroll anchor — under them. The
 * caller offers "show newer" and flips `follow` back explicitly.
 */
export function useStableWatermark(liveSeq: Seq | null, follow: boolean): Seq | null {
  const [pinned, setPinned] = useState<Seq | null>(liveSeq)
  // Adjusting state during render (not in an effect) keeps the first paint on the right watermark.
  if (follow && pinned !== liveSeq) setPinned(liveSeq)
  if (!follow && pinned === null && liveSeq !== null) setPinned(liveSeq)
  return follow ? liveSeq : pinned
}
