import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import type { Position, SyncSnapshot } from "../../api/sync"
import { CHECKPOINT_REJECTION_LABELS, labelKey, SYNC_ERROR_LABELS } from "../../constants/labels"
import { gtSeq } from "../../utils/seq"

interface SyncNoticeProps {
  snapshot: SyncSnapshot
  position: Position | null
  onReturnToLive: () => void
}

interface NoticeProps {
  tone: "info" | "warn" | "danger"
  children: ReactNode
}

const TONES = {
  info: "bg-hairsoft text-n700",
  warn: "bg-a100 text-a700",
  danger: "bg-dangersoft text-dangerink",
} as const

function Notice({ tone, children }: NoticeProps) {
  return (
    <p
      role={tone === "info" ? "status" : "alert"}
      className={`${TONES[tone]} flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-xs`}
    >
      {children}
    </p>
  )
}

/**
 * The honest state of the local copy: still opening, catching up to the
 * committed head, replayed from the start because a checkpoint was refused,
 * holding only a verified prefix after an error, or containing event versions
 * this viewer cannot interpret.
 */
export function SyncNotice({ snapshot, position, onReturnToLive }: SyncNoticeProps) {
  const { t } = useTranslation("admin-trajectories")
  const { phase, error, rejection } = snapshot
  const unsupported = position?.status === "ready" ? position.state.unsupported_events : []
  const catchingUp = phase === "live" && gtSeq(snapshot.headSeq, snapshot.loadedSeq)
  return (
    <div className="flex flex-col gap-2 empty:hidden" data-testid="trajectory-sync-notice" data-phase={phase}>
      {phase === "opening" && (
        <Notice tone="info">
          <Spinner className="size-3.5" />
          {t(error?.kind === "network" ? "sync.retrying" : "sync.opening")}
        </Notice>
      )}
      {phase === "denied" && <Notice tone="danger">{t("sync.denied")}</Notice>}
      {phase === "gone" && <Notice tone="danger">{t("sync.gone")}</Notice>}
      {phase === "live" && error && (
        <Notice tone={error.kind === "network" ? "warn" : "danger"}>
          {t(labelKey(SYNC_ERROR_LABELS, error.kind, "sync.error.other"), {
            value: error.kind,
            seq: error.seq ?? snapshot.loadedSeq,
          })}
        </Notice>
      )}
      {catchingUp && (
        <Notice tone="info">
          <Spinner className="size-3.5" />
          {t("sync.catchingUp", { loaded: snapshot.loadedSeq, head: snapshot.headSeq })}
        </Notice>
      )}
      {rejection && (
        <Notice tone="warn">
          {t(labelKey(CHECKPOINT_REJECTION_LABELS, rejection, "sync.rejection.other"), { value: rejection })}
        </Notice>
      )}
      {unsupported.length > 0 && (
        <Notice tone="warn">
          {t("sync.unsupported", { count: unsupported.length, seq: unsupported[0].seq })}
        </Notice>
      )}
      {position?.status === "error" && (
        <Notice tone="danger">
          {t(labelKey(SYNC_ERROR_LABELS, position.error.kind, "sync.error.other"), {
            value: position.error.kind,
            seq: position.seq,
          })}
          <button type="button" onClick={onReturnToLive} className="underline">
            {t("playback.returnToLive")}
          </button>
        </Notice>
      )}
    </div>
  )
}
