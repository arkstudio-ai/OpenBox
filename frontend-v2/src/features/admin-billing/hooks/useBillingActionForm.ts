import { useRef, useState } from "react"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import { useManageBilling } from "@/features/admin-billing/api/admin-billing"
import type {
  BillingActionKind,
  BillingWrite,
  SubscriptionTerm,
  WorkspaceBillingDetail,
} from "@/features/admin-billing/types"

function pendingKey(userId: string, workspaceId: string) {
  return `admin-billing-pending:${userId}:${workspaceId}`
}

export function pendingBillingWrite(userId: string, workspaceId: string): BillingWrite | null {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(pendingKey(userId, workspaceId)) ?? "null",
    ) as BillingWrite | null
    return value?.body?.request_key &&
      value.actorId === userId &&
      value.workspaceId === workspaceId &&
      ["credits", "grant", "change", "cancel"].includes(value.kind)
      ? value
      : null
  } catch {
    return null
  }
}

function localInput(iso?: string | null) {
  if (!iso) return ""
  const date = new Date(iso)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

export interface BillingFormProps {
  detail: WorkspaceBillingDetail
  kind: BillingActionKind
  term?: SubscriptionTerm
  pending?: BillingWrite | null
  onDone: () => void
}

function validDraft(kind: BillingActionKind, credits: string, reason: string, end: string) {
  if (!reason.trim()) return false
  if (kind === "credits")
    return /^\d+(\.\d{1,6})?$/.test(credits) && Number(credits) > 0 && Number(credits) <= 1_000_000
  if (kind === "change") return end !== "" && Number.isFinite(new Date(end).getTime())
  return true
}

export function useBillingActionForm({ detail, kind, term, pending, onDone }: BillingFormProps) {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const mutation = useManageBilling(detail.workspace.id)
  const [credits, setCredits] = useState(pending?.body.credits ?? "")
  const [plan, setPlan] = useState(pending?.body.plan_id ?? term?.plan_id ?? "pro")
  const [cycle, setCycle] = useState(pending?.body.cycle ?? "monthly")
  const [end, setEnd] = useState(localInput(pending?.body.ends_at ?? term?.ends_at))
  const [reason, setReason] = useState(pending?.body.reason ?? "")
  const [confirmed, setConfirmed] = useState(Boolean(pending))
  const [frozen, setFrozen] = useState<BillingWrite | null>(pending ?? null)
  const submitted = useRef<BillingWrite | null>(pending ?? null)
  const inFlight = useRef(false)
  const busy = mutation.isPending
  const fixed = busy || frozen !== null
  const valid = validDraft(kind, credits, reason, end)
  const errorCode =
    mutation.error instanceof ApiError && mutation.error.status < 500 ? mutation.error.code : "UNKNOWN_RESULT"
  const store = (write: BillingWrite | null) => {
    try {
      const key = pendingKey(userId, detail.workspace.id)
      if (write) sessionStorage.setItem(key, JSON.stringify(write))
      else sessionStorage.removeItem(key)
    } catch {
      /* Same mounted dialog still retains the retry key. */
    }
  }
  const submit = async () => {
    if (!valid || !confirmed || inFlight.current) return
    inFlight.current = true
    const write: BillingWrite = submitted.current ?? {
      actorId: userId,
      workspaceId: detail.workspace.id,
      kind,
      subscriptionId: term?.id,
      body: {
        request_key: crypto.randomUUID(),
        reason: reason.trim(),
        ...(kind === "credits" ? { credits } : {}),
        ...(kind === "grant" ? { plan_id: plan, cycle } : {}),
        ...(kind === "change"
          ? {
              plan_id: plan,
              ends_at:
                end === localInput(term?.ends_at) && term?.ends_at
                  ? term.ends_at
                  : new Date(end).toISOString(),
            }
          : {}),
        ...(kind === "change" || kind === "cancel" ? { expected_revision: term?.revision } : {}),
      },
    }
    submitted.current = write
    setFrozen(write)
    store(write)
    try {
      await mutation.mutateAsync(write)
      store(null)
      onDone()
    } catch (error) {
      // A timeout/5xx may follow a successful commit. Retain the exact payload
      // across retries, closing the dialog and reloads of this browser tab.
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        store(null)
        setFrozen(null)
        submitted.current = null
      }
    } finally {
      inFlight.current = false
    }
  }

  return {
    credits,
    setCredits,
    plan,
    setPlan,
    cycle,
    setCycle,
    end,
    setEnd,
    reason,
    setReason,
    confirmed,
    setConfirmed,
    frozen,
    busy,
    fixed,
    valid,
    errorCode,
    error: mutation.error,
    submit,
  }
}
