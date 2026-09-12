// A range input only holds numbers, but a seq may exceed Number's exact range.
// The slider therefore works in steps across [floor, ceiling] and every
// conversion goes through BigInt, so a thumb position always names a real seq.
import type { Seq } from "../../types/protocol"

export const SLIDER_STEPS = 10_000

function span(floor: Seq, ceiling: Seq): bigint {
  const value = BigInt(ceiling) - BigInt(floor)
  return value > 0n ? value : 0n
}

export function sliderMax(floor: Seq, ceiling: Seq): number {
  const range = span(floor, ceiling)
  return range < BigInt(SLIDER_STEPS) ? Number(range) : SLIDER_STEPS
}

export function seqToSlider(seq: Seq, floor: Seq, ceiling: Seq): number {
  const range = span(floor, ceiling)
  if (range === 0n) return 0
  let offset = BigInt(seq) - BigInt(floor)
  if (offset < 0n) offset = 0n
  if (offset > range) offset = range
  const steps = BigInt(sliderMax(floor, ceiling))
  return Number((offset * steps) / range)
}

export function sliderToSeq(value: number, floor: Seq, ceiling: Seq): Seq {
  const range = span(floor, ceiling)
  const steps = BigInt(sliderMax(floor, ceiling))
  if (range === 0n || steps === 0n) return BigInt(floor).toString()
  const clamped = BigInt(Math.max(0, Math.min(Number(steps), Math.round(value))))
  return (BigInt(floor) + (clamped * range) / steps).toString()
}
