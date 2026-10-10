// The call's one moving part: a dot that swells with whoever is talking and
// breathes while the assistant thinks or works. Static under reduced motion.
import { useRef } from "react"
import { cn } from "@/shared/lib/cn"
import { useLevelVariable } from "../hooks/useVoiceCall"
import type { CallStatus, Phase } from "../lib/types"

export interface VoiceOrbProps {
  status: CallStatus
  phase: Phase
  muted?: boolean
  size: "sm" | "md"
}

const PHASE_TONE: Record<Phase, string> = {
  greeting: "bg-accent",
  listening: "bg-s600",
  thinking: "bg-n500",
  speaking: "bg-accent",
  working: "bg-a700",
}

interface OrbMotion {
  /** Whose loudness scales the orb. */
  level: "mic" | "out" | null
  breathing: boolean
}

function orbMotion(status: CallStatus, phase: Phase, muted: boolean): OrbMotion {
  if (status === "requesting_mic" || status === "connecting") return { level: null, breathing: true }
  if (status !== "connected") return { level: null, breathing: false }
  if (phase === "listening") return { level: muted ? null : "mic", breathing: false }
  if (phase === "speaking" || phase === "greeting") return { level: "out", breathing: false }
  return { level: null, breathing: true }
}

export function VoiceOrb({ status, phase, muted = false, size }: VoiceOrbProps) {
  const ref = useRef<HTMLSpanElement>(null)
  const motion = orbMotion(status, phase, muted)
  useLevelVariable(ref, motion.level)
  const tone = status === "connected" ? PHASE_TONE[phase] : "bg-n400"
  return (
    <span
      ref={ref}
      aria-hidden
      className={cn(
        "relative inline-flex flex-none rounded-full",
        size === "md" ? "size-7" : "size-5",
        // 1.0–1.35× with the level, eased so 50 ms updates do not jitter.
        motion.level &&
          "motion-safe:[transform:scale(calc(1_+_var(--level,0)*0.35))] motion-safe:transition-transform motion-safe:duration-100",
        motion.breathing && "motion-safe:animate-pulse-dot motion-safe:[animation-duration:1.6s]",
      )}
    >
      <span className={cn("absolute inset-0 rounded-full opacity-25", tone)} />
      <span className={cn("absolute inset-1/4 rounded-full", tone)} />
    </span>
  )
}
